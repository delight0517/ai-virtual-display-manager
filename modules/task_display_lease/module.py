"""Manager-owned Workbench v2 task display lease IPC adapter."""
from __future__ import annotations

import hashlib
import json
import sys
import threading
from pathlib import Path

from modules.only_chat_debug_display.module import NamedPipeServer
from modules.task_display_lease.authority import (
    TaskDisplayLeaseAuthority, API_VERSION as LEASE_PROTOCOL_VERSION, VERSION,
)
from modules.task_display_lease.replay_store import ReplayNonceStore

MODULE_ID = "task_display_lease"
API_VERSION = 1
PIPE_PATH = r"\\.\pipe\OnlyChatAiTaskDisplayLease.v2"
MAX_REQUEST_BYTES = 8192
MAX_REPLY_BYTES = 8192
APP_PATH_REGISTRY = (Path.home() / "Desktop" / "gdrive" / "My Drive" /
                     "Codex App Registry" / "APP_PATH_REGISTRY.json")
WORKBENCH_ID = "enderman-qa-workbench"
WORKBENCH_INSTANCE_ID = "696236b53e7540ebac6c853b8e0d62e3"


def registered_workbench_exe(registry_path=APP_PATH_REGISTRY):
    try:
        data = json.loads(Path(registry_path).read_text(encoding="utf-8"))
        entries = data.get("apps") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return None
        for item in entries:
            if not isinstance(item, dict) or item.get("id") != WORKBENCH_ID:
                continue
            windows = item.get("windows")
            if (item.get("verified") is not True or not isinstance(windows, dict)
                    or windows.get("installedVerified") is not True):
                return None
            path = Path(windows.get("installed", "")) / "EndermanQa.Desktop.exe"
            digest = str(windows.get("installedExeSha256", "")).upper()
            if not path.is_file() or len(digest) != 64:
                return None
            actual = hashlib.sha256(path.read_bytes()).hexdigest().upper()
            return path if actual == digest else None
    except (OSError, ValueError, TypeError):
        return None
    return None


class TaskDisplayLeaseModule:
    def __init__(self, *, pool=None, topology_reader=None, server_factory=None,
                 registry_path=APP_PATH_REGISTRY, process_image_checker=None,
                 monotonic=None, utc_now=None, id_factory=None,
                 replay_store_factory=ReplayNonceStore):
        self._pool = pool
        self._topology_reader = topology_reader
        self._server_factory = server_factory or NamedPipeServer
        self._registry_path = Path(registry_path)
        self._process_image_checker = process_image_checker
        self._monotonic = monotonic
        self._utc_now = utc_now
        self._id_factory = id_factory
        self._replay_store_factory = replay_store_factory
        self._replay_store = None
        self._server = None
        self._authority = None
        self._lock = threading.RLock()
        self._startup_error = None

    def _peer_authorized(self, pid):
        path = registered_workbench_exe(self._registry_path)
        if path is None:
            return False
        checker = self._process_image_checker
        if checker is None:
            from modules.only_chat_task_runtime.module import process_image_matches
            checker = process_image_matches
        try:
            return checker(pid, path) is True
        except Exception:
            return False

    def _pool_keys(self, topology):
        if self._pool is None or not isinstance(topology, dict):
            return set()
        from modules.virtual_output_pool.module import mode_identity_key
        modes = {}
        for row in topology.get("displays", []):
            if not isinstance(row, dict):
                return set()
            try:
                key = mode_identity_key(row.get("display_name"),
                                       row.get("monitor_device_id"), row.get("monitor_path"))
                modes[key] = (int(row.get("width")), int(row.get("height")),
                              int(row.get("refresh_rate")))
            except (TypeError, ValueError):
                return set()
        state = self._pool.task_leaseable_endpoint_keys(modes)
        return set(state.get("endpoint_keys", [])) if state.get("eligible") is True else set()

    def _heartbeat(self, lease_id):
        if self._pool is None:
            raise RuntimeError("Output pool unavailable")
        self._pool.heartbeat(lease_id, confirmed=True)

    def _release(self, lease_id):
        if self._pool is not None:
            self._pool.release(lease_id)

    def bind_dependencies(self, *, pool, topology_reader):
        if pool is None or not callable(topology_reader):
            raise TypeError("A configured virtual-output pool and topology reader are required")
        with self._lock:
            if self._authority and self._authority._leases:
                raise RuntimeError("Cannot rebind dependencies while task leases are active")
            self._pool = pool
            self._topology_reader = topology_reader
            # init() runs during ModuleHost.load() before app.py wires the pool.
            # Replace the authority's placeholder reader or every acquire sees {}.
            if self._authority is not None:
                self._authority.topology_reader = topology_reader

    def init(self):
        if self._authority is not None or self._server is not None:
            raise RuntimeError("Task display lease provider already initialized")
        try:
            self._replay_store = self._replay_store_factory()
        except Exception as exc:
            self._startup_error = f"REPLAY_GUARD_UNAVAILABLE:{type(exc).__name__}"
            return
        self._authority = TaskDisplayLeaseAuthority(
            consumer_id=WORKBENCH_ID, instance_id=WORKBENCH_INSTANCE_ID,
            peer_authorized=self._peer_authorized,
            pool_eligible_endpoints=self._pool_keys,
            topology_reader=self._topology_reader or (lambda: {}),
            heartbeat=self._heartbeat, release_pool_lease=self._release,
            consume_replay=self._replay_store.consume,
            **({"monotonic": self._monotonic} if self._monotonic else {}),
            **({"utc_now": self._utc_now} if self._utc_now else {}),
            **({"id_factory": self._id_factory} if self._id_factory else {}))
        if "--self-check" in sys.argv:
            self._startup_error = "BROKER_NOT_STARTED_IN_SELF_CHECK"
            return
        try:
            self._server = self._server_factory(
                lambda _request: self._failure(None, "CLIENT_AUTH_REQUIRED"),
                pipe_path=PIPE_PATH, max_request_bytes=MAX_REQUEST_BYTES,
                max_reply_bytes=MAX_REPLY_BYTES,
                client_authenticator=self._peer_authorized,
                rejected_handler=lambda request: self._failure(request, "CONSUMER_PROCESS_UNAUTHORIZED"),
                authenticated_handler=self._handle_authenticated)
            self._server.start()
            self._startup_error = None
        except Exception as exc:
            self._startup_error = f"{type(exc).__name__}: {exc}"
            try:
                if self._server:
                    self._server.stop()
            except Exception:
                pass
            self._server = None

    def _handle_authenticated(self, request, peer_pid):
        authority = self._authority
        if authority is None:
            return self._failure(request, "PROVIDER_NOT_READY")
        return authority.handle(request, peer_pid)

    @staticmethod
    def _failure(request, code):
        return {"version": LEASE_PROTOCOL_VERSION,
                "request_id": request.get("request_id") if isinstance(request, dict) else None,
                "nonce": request.get("nonce") if isinstance(request, dict) else None,
                "state": "denied", "error_code": code}

    def refresh(self):
        server = self._server
        pipe_ready = bool(server and server.ready)
        dependencies_ready = self._pool is not None and callable(self._topology_reader)
        provider_ready = pipe_ready and bool(self._authority) and dependencies_ready
        return {"pipe_path": PIPE_PATH, "pipe_ready": pipe_ready,
                "provider_available": provider_ready,
                "protocol_version": LEASE_PROTOCOL_VERSION,
                "active_lease_count": len(self._authority._leases) if self._authority else 0,
                "error_code": (None if provider_ready else self._startup_error or
                               "DEPENDENCIES_NOT_BOUND" if pipe_ready else
                               self._startup_error or "BROKER_UNAVAILABLE"),
                "lease_acquisition": ("requires_active_pool_created_unmirrored_output"
                                      if provider_ready else "fail_closed")}

    def reap_expired(self):
        authority = self._authority
        return authority.reap_expired() if authority else 0

    def action(self, name, _payload):
        if name != "lease_status":
            raise ValueError("Unknown task display lease action")
        return self.refresh()

    def snapshot(self):
        if self._server and self._server.active_clients:
            raise RuntimeError("A lease request is active; reload is deferred")
        if self._server and self._server.ready:
            raise RuntimeError("The process-global lease pipe requires manager restart to reload")
        return {"inactive": True}

    def restore(self, state):
        if state not in (None, {"inactive": True}):
            raise ValueError("Active leases cannot be restored across module reload")

    def reload_blocked(self):
        return bool(self._server and self._server.active_clients)

    def dispose(self):
        with self._lock:
            if self._authority and self._authority._leases:
                raise RuntimeError("Active task display leases prevent provider shutdown")
            if self._server:
                self._server.stop()
            self._server = None
            self._authority = None


def create():
    return TaskDisplayLeaseModule()
