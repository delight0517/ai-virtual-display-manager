"""Bounded, thread-safe visibility mailbox; Tk stays on its owner thread."""
from threading import Lock


class BackgroundUI:
    def __init__(self):
        self._lock = Lock()
        self._pending = None
        self._visible = False
        self._last_error = None

    def request(self, action):
        if action not in {"show", "hide", "stop", "status"}:
            raise ValueError("UI_ACTION_INVALID")
        with self._lock:
            if action != "status":
                if self._pending is not None:
                    raise RuntimeError("UI_REQUEST_PENDING")
                self._pending = action
            if action != "status":
                self._last_error = None
            return {"mode": "background_service", "visible": self._visible,
                    "pending": self._pending, "accepted": action != "status",
                    "last_error": self._last_error}

    def take(self):
        with self._lock:
            action, self._pending = self._pending, None
            return action

    def publish(self, visible):
        with self._lock:
            self._visible = bool(visible)

    def fail(self, error_code):
        with self._lock:
            self._pending = None
            self._last_error = str(error_code)[:80]
