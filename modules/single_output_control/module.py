"""Explicit one-output controller, isolated from the 2+ output lease pool."""
from __future__ import annotations

API_VERSION = 1
MODULE_ID = "single_output_control"
VERSION = "0.1.0"
ACTIONS = ("preview", "create")
EXPECTED_DISPLAY_NAME = r"\\.\DISPLAY10"


def validate_state(state: dict) -> dict:
    if not isinstance(state, dict):
        raise RuntimeError("Could not read a verified display state")
    if state.get("topology_verified") is not True:
        raise RuntimeError("Active display topology is not verified")
    device_id = state.get("mtt_device_instance_id")
    if not isinstance(device_id, str) or not device_id.strip():
        raise RuntimeError("Exact MTT device identity is unavailable")
    if state.get("pool_state") != "off":
        raise RuntimeError("The managed output pool or a recovery operation is active")
    outputs = state.get("outputs")
    if not isinstance(outputs, list):
        raise RuntimeError("MTT output inventory is unavailable")
    if len(outputs) > 1:
        raise RuntimeError("More than one MTT output already exists; preserving current topology")
    if outputs:
        output = outputs[0]
        if (not isinstance(output, dict)
                or output.get("display_name", "").casefold() != EXPECTED_DISPLAY_NAME.casefold()
                or output.get("topology_verified") is not True
                or output.get("cloned") is not False
                or output.get("monitor_parent", "").casefold() != device_id.casefold()):
            raise RuntimeError("The existing MTT output is not the exact verified DISPLAY10 endpoint")
    return {"mtt_device_instance_id": device_id,
            "output_count": len(outputs),
            "display_name": outputs[0]["display_name"] if outputs else None,
            "already_ready": len(outputs) == 1}


class SingleOutputControlModule:
    def __init__(self):
        self._read_state = None
        self._set_count = None

    def init(self):
        pass

    def dispose(self):
        self._read_state = None
        self._set_count = None

    def snapshot(self):
        return {}

    def restore(self, _state):
        pass

    def bind(self, *, read_state, set_count):
        if not callable(read_state) or not callable(set_count):
            raise TypeError("Single-output backend callbacks are required")
        self._read_state, self._set_count = read_state, set_count

    def reload_blocked(self):
        return False

    def _current(self):
        if not self._read_state:
            raise RuntimeError("Single-output backend is not configured")
        return validate_state(self._read_state())

    def action(self, action, payload):
        if action not in ACTIONS:
            raise ValueError("Unsupported single-output action")
        current = self._current()
        if action == "preview":
            return {"state": "ready" if current["already_ready"] else "confirm_required",
                    **current,
                    "impact": "Add exactly one MTT virtual output without changing its resolution or physical outputs.",
                    "identity_limit": "Windows assigns the GDI display name during activation; only DISPLAY10 is accepted for QA."}
        if payload.get("confirmed") is not True:
            raise RuntimeError("An explicit in-app confirmation is required")
        if current["already_ready"]:
            return {"state": "ready", **current}
        if not self._set_count:
            raise RuntimeError("Single-output backend is not configured")
        self._set_count(1)
        verified = self._current()
        if not verified["already_ready"]:
            raise RuntimeError("The requested single MTT output did not verify; preserving the observed configuration")
        return {"state": "ready", **verified}


def create():
    return SingleOutputControlModule()
