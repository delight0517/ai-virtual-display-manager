"""Exact app-owned startup registration and durable developer-editing gate.

The Run value dispatches --startup. Pauses suppress that dispatcher, rather than
deleting registrations or touching any other app. Absolute expiry is rechecked
after sleep/logon; the waiting dispatcher holds no windows or input hooks.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "AI Virtual Display Manager"


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".resident-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


class RunRegistration:
    def read(self):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, kind = winreg.QueryValueEx(key, RUN_NAME)
                if kind != winreg.REG_SZ or not isinstance(value, str):
                    raise RuntimeError("Startup registration has an unknown type")
                return value
        except FileNotFoundError:
            return None

    def write(self, command):
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, command)

    def remove(self):
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_NAME)


class ResidentController:
    def __init__(self, root, pythonw, state_path=None, registration=None, clock=time.time,
                 frozen_executable=None):
        self.root = Path(root).resolve()
        self.pythonw = Path(pythonw).resolve()
        self.frozen_executable = (Path(frozen_executable).resolve()
                                  if frozen_executable is not None else None)
        self.command = subprocess.list2cmdline(self.startup_argv())
        self.path = Path(state_path) if state_path else Path(os.environ["LOCALAPPDATA"]) / RUN_NAME / "resident.json"
        self.registration = registration if registration is not None else RunRegistration()
        self.clock = clock

    def startup_argv(self):
        """Return the exact background dispatcher for this source or frozen build."""
        if self.frozen_executable is not None:
            return [str(self.frozen_executable), "--startup"]
        return [str(self.pythonw), "-B", str(self.root / "app.py"), "--startup"]

    def _read(self):
        if not self.path.exists():
            return {"schema": 1, "pause": None}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema") != 1 or "pause" not in value:
            raise RuntimeError("Invalid developer-pause state; startup refused")
        pause = value["pause"]
        if pause is not None:
            if (not isinstance(pause, dict) or not isinstance(pause.get("task_id"), str)
                    or not pause.get("owner") or type(pause.get("expires")) not in (int, float)
                    or type(pause.get("started")) not in (int, float)
                    or pause["expires"] <= pause["started"]
                    or pause.get("registration") not in (None, self.command)):
                raise RuntimeError("Invalid developer-pause receipt; startup refused")
        return value

    def status(self):
        current = self.registration.read()
        state = self._read()
        pause = state["pause"]
        if pause and self.clock() >= pause["expires"]:
            if current != pause["registration"]:
                raise RuntimeError("Startup registration changed during pause; recovery needs review")
            state.update(pause=None, last_pause={**pause, "restored": self.clock()})
            atomic_json(self.path, state)
            pause = None
        return {"startup_enabled": current == self.command,
                "startup_conflict": current not in (None, self.command),
                "developer_paused": pause is not None, "pause": pause,
                "registration": RUN_NAME}

    def enable(self):
        state = self.status()
        if state["startup_conflict"] or state["developer_paused"]:
            raise RuntimeError("Startup ownership conflict or active developer pause")
        self.registration.write(self.command)
        if self.registration.read() != self.command:
            raise RuntimeError("Startup registration verification failed")
        return self.status()

    def disable(self):
        current = self.registration.read()
        if current not in (None, self.command):
            raise RuntimeError("Refusing to remove a changed startup registration")
        if current is not None:
            self.registration.remove()
        # A subsequent deliberate opt-out cancels the old restoration intent.
        state = self._read()
        state.update(pause=None, disabled_at=self.clock())
        atomic_json(self.path, state)
        return self.status()

    def pause(self, task_id, seconds=10800, owner="developer"):
        if not isinstance(task_id, str) or not task_id.strip() or not 1 <= seconds <= 86400:
            raise ValueError("A task ID and bounded pause duration are required")
        state = self.status()
        if state["startup_conflict"] or state["developer_paused"]:
            raise RuntimeError("Existing pause or startup ownership conflict")
        now = self.clock()
        atomic_json(self.path, {"schema": 1, "pause": {
            "task_id": task_id, "owner": owner, "started": now,
            "expires": now + seconds, "registration": self.registration.read()}})
        return self.status()

    def resume(self):
        value = self._read()
        pause = value["pause"]
        if pause and self.registration.read() != pause["registration"]:
            raise RuntimeError("Registration changed; preserving external changes and pause receipt")
        value.update(pause=None, resumed_at=self.clock())
        atomic_json(self.path, value)
        return self.status()

    def startup_allowed(self):
        state = self.status()
        return state["startup_enabled"] and not state["developer_paused"]
