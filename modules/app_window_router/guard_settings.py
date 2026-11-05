"""Atomic opt-in preference for the visible physical-window guard."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from threading import RLock


class GuardSettingsStore:
    KEY = "physical_window_guard_enabled"

    def __init__(self, path: str | Path | None = None):
        if path is None:
            local_app_data = os.environ.get("LOCALAPPDATA")
            if not local_app_data:
                raise RuntimeError("LOCALAPPDATA is unavailable; guard preference cannot be saved")
            path = Path(local_app_data) / "AI Virtual Display Manager" / "settings.json"
        self.path = Path(path)
        self._lock = RLock()

    def read(self) -> tuple[bool, str | None]:
        with self._lock:
            if not self.path.exists():
                return False, None
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                return False, f"{type(exc).__name__}: settings file could not be read"
            if not isinstance(raw, dict):
                return False, "Settings file is not an object"
            value = raw.get(self.KEY, False)
            if type(value) is not bool:
                return False, "Physical-window guard setting is not a boolean"
            return value, None

    def write(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        with self._lock:
            data = {}
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("Settings file is not an object")
            data[self.KEY] = enabled
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                        mode="w", encoding="utf-8", newline="\n",
                        dir=self.path.parent, prefix=".settings-", suffix=".tmp",
                        delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump(data, stream, ensure_ascii=False, sort_keys=True, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            except Exception:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
                raise
