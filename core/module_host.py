"""Versioned, replaceable feature module loader with state-preserving rollback."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

API_VERSION = 1


@dataclass
class Loaded:
    module_id: str
    path: Path
    version: str
    digest: str
    module: ModuleType
    instance: Any
    generation: int


class ModuleHost:
    def __init__(self, canonical_root: Path):
        self.root = canonical_root.resolve()
        self.manifest_path = self.root / "module-registry.json"
        self._manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self._loaded: dict[str, Loaded] = {}
        self._lock = threading.RLock()
        self._operation_changed = threading.Condition(self._lock)
        self.active_operations = 0
        self._operation_thread_id: int | None = None
        self._exclusive_depth = 0

    def _candidate(self, module_id: str, path: Path):
        path = path.resolve(strict=True)
        if not path.is_relative_to(self.root):
            raise ValueError("Feature module must be inside the canonical app root")
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        name = f"_aivdm_{module_id}_{digest[:16]}"
        spec = importlib.util.spec_from_file_location(name, path)
        if not spec or not spec.loader:
            raise ImportError(f"Cannot load module: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(name, None)
            raise
        if getattr(module, "API_VERSION", None) != API_VERSION:
            sys.modules.pop(name, None)
            raise ValueError("Feature API version mismatch")
        if getattr(module, "MODULE_ID", None) != module_id:
            sys.modules.pop(name, None)
            raise ValueError("Feature ID mismatch")
        declared = self._manifest_entry(module_id)
        if getattr(module, "VERSION", None) != declared.get("version"):
            sys.modules.pop(name, None)
            raise ValueError("Feature version does not match the module registry")
        return path, digest, module

    def _manifest_entry(self, module_id: str):
        entries = [entry for entry in self._manifest.get("modules", []) if entry.get("id") == module_id]
        if len(entries) != 1 or entries[0].get("apiVersion") != API_VERSION:
            raise ValueError("Feature is missing from the module registry or has an incompatible API")
        return entries[0]

    def load(self, module_id: str, path: Path | None = None):
        with self._lock:
            if self.active_operations:
                raise RuntimeError("Another manager operation is already active")
            if module_id in self._loaded:
                raise ValueError(f"Module already loaded: {module_id}")
            declared = self._manifest_entry(module_id)
            registered_path = (self.root / declared["path"]).resolve()
            if path is not None and path.resolve() != registered_path:
                raise ValueError("Requested module path differs from the registered canonical path")
            path = registered_path
            path, digest, module = self._candidate(module_id, path)
            instance = module.create()
            try:
                instance.init()
            except Exception:
                instance.dispose()
                sys.modules.pop(module.__name__, None)
                raise
            loaded = Loaded(module_id, path, module.VERSION, digest, module, instance, 1)
            self._loaded[module_id] = loaded
            return self.info(module_id)

    def reload(self, module_id: str):
        with self._lock:
            if self.active_operations:
                raise RuntimeError("An active driver operation prevents module reload")
            old = self._loaded[module_id]
            blocked = getattr(old.instance, "reload_blocked", None)
            if callable(blocked) and blocked():
                raise RuntimeError(f"Active state prevents reload of module: {module_id}")
            state = old.instance.snapshot()
            path, digest, candidate = self._candidate(module_id, old.path)
            new_instance = candidate.create()
            try:
                new_instance.restore(state)
                new_instance.init()
            except Exception:
                try:
                    new_instance.dispose()
                finally:
                    sys.modules.pop(candidate.__name__, None)
                raise  # Previous instance stays loaded and usable.
            self._loaded[module_id] = Loaded(module_id, path, candidate.VERSION, digest,
                                             candidate, new_instance, old.generation + 1)
            old.instance.dispose()
            sys.modules.pop(old.module.__name__, None)
            return self.info(module_id)

    def snapshot(self, module_id: str):
        with self._lock:
            if self.active_operations:
                raise RuntimeError("Another manager operation is already active")
            return self._loaded[module_id].instance.snapshot()

    def refresh(self, module_id: str, *, wait_timeout: float = 0):
        if (isinstance(wait_timeout, bool) or not isinstance(wait_timeout, (int, float))
                or not 0 <= wait_timeout <= 30 or not math.isfinite(wait_timeout)):
            raise ValueError("wait_timeout must be a finite number from 0 to 30 seconds")
        with self._operation_changed:
            # Pool actions call their injected topology readers synchronously.
            # Preserve same-thread nesting. Other callers may wait briefly for
            # the manager-owned operation to release its module instances.
            if (self.active_operations
                    and self._operation_thread_id != threading.get_ident()
                    and (wait_timeout == 0 or not self._operation_changed.wait_for(
                        lambda: self.active_operations == 0, timeout=wait_timeout))):
                raise RuntimeError("Another manager operation is already active")
            instance = self._loaded[module_id].instance
            self.active_operations += 1
            self._operation_thread_id = threading.get_ident()
            self._exclusive_depth += 1
        # Device discovery may wait on a subprocess. Keep the host lock free so
        # UI/status and shutdown callers can reject promptly, rather than wait
        # behind that subprocess. The reservation guards the captured instance
        # from reload/disposal until its refresh has actually finished.
        try:
            return instance.refresh()
        finally:
            self._finish_operation(exclusive=True)

    def module_info(self, module_id: str):
        return self.info(module_id)

    def command(self, module_id: str, command: str):
        with self._lock:
            if self.active_operations:
                raise RuntimeError("A driver operation is already active")
            self.active_operations += 1
            self._operation_thread_id = threading.get_ident()
            self._exclusive_depth += 1
        try:
            return self._loaded[module_id].instance.command(command)
        finally:
            self._finish_operation(exclusive=True)

    def device_action(self, module_id: str, action: str):
        with self._lock:
            if self.active_operations:
                raise RuntimeError("A driver operation is already active")
            self.active_operations += 1
            self._operation_thread_id = threading.get_ident()
            self._exclusive_depth += 1
        try:
            return self._loaded[module_id].instance.device_action(action)
        finally:
            self._finish_operation(exclusive=True)

    def invoke(self, module_id: str, action: str, payload: dict[str, Any] | None = None,
               *, wait_timeout: float = 0):
        """Call an exported action, optionally waiting boundedly off the UI thread.

        The default preserves prompt busy rejection. Condition.wait_for releases
        the host lock so the reservation owner can finish or accept cancellation.
        """
        if (isinstance(wait_timeout, bool) or not isinstance(wait_timeout, (int, float))
                or not 0 <= wait_timeout <= 30 or not math.isfinite(wait_timeout)):
            raise ValueError("wait_timeout must be a finite number from 0 to 30 seconds")
        with self._operation_changed:
            if self.active_operations:
                if (wait_timeout == 0 or self._operation_thread_id == threading.get_ident()
                        or not self._operation_changed.wait_for(
                            lambda: self.active_operations == 0, timeout=wait_timeout)):
                    raise RuntimeError("Another manager operation is already active")
            loaded = self._loaded[module_id]
            allowed = tuple(getattr(loaded.module, "ACTIONS", ()))
            if action not in allowed:
                raise ValueError("Action is not exported by this feature module")
            handler = getattr(loaded.instance, "action", None)
            if not callable(handler):
                raise RuntimeError("Loaded feature module has no action handler")
            self.active_operations += 1
            self._operation_thread_id = threading.get_ident()
            self._exclusive_depth += 1
        try:
            return handler(action, dict(payload or {}))
        finally:
            self._finish_operation(exclusive=True)

    def invoke_concurrent(self, module_id: str, action: str,
                          payload: dict[str, Any] | None = None):
        """Dispatch only a trusted module's explicitly concurrency-safe actions.

        An additional reservation prevents reload/disposal while this callback
        runs. It must not replace the ordinary operation's thread identity: that
        owner may still need its synchronous nested topology refresh.
        """
        with self._lock:
            loaded = self._loaded[module_id]
            allowed = tuple(getattr(loaded.module, "ACTIONS", ()))
            concurrent = tuple(getattr(loaded.module, "CONCURRENT_ACTIONS", ()))
            if action not in allowed or action not in concurrent:
                raise ValueError("Action is not exported for concurrent dispatch by this feature module")
            handler = getattr(loaded.instance, "action", None)
            if not callable(handler):
                raise RuntimeError("Loaded feature module has no action handler")
            self.active_operations += 1
        try:
            return handler(action, dict(payload or {}))
        finally:
            self._finish_operation(exclusive=False)

    def _finish_operation(self, *, exclusive: bool):
        with self._operation_changed:
            self.active_operations -= 1
            if exclusive:
                self._exclusive_depth -= 1
                if not self._exclusive_depth:
                    self._operation_thread_id = None
            self._operation_changed.notify_all()

    def info(self, module_id: str):
        item = self._loaded[module_id]
        return {"module_id": item.module_id, "version": item.version, "sha256": item.digest,
                "generation": item.generation, "api_version": API_VERSION}

    def dispose_all(self):
        with self._lock:
            if self.active_operations:
                raise RuntimeError("Another manager operation is already active")
            blocked_ids = [item.module_id for item in self._loaded.values()
                           if callable(getattr(item.instance, "reload_blocked", None))
                           and item.instance.reload_blocked()]
            if blocked_ids:
                raise RuntimeError("Active module state prevents manager disposal: "
                                   + ", ".join(sorted(blocked_ids)))
            for item in list(self._loaded.values()):
                item.instance.dispose()
                sys.modules.pop(item.module.__name__, None)
            self._loaded.clear()
