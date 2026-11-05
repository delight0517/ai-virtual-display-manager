"""Windows Primary Screen Cursor Guard.
Locks the user mouse cursor to the primary physical screen boundaries so it cannot cross into the virtual AI display.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import threading
import time

from core.win32_dpi import physical_pixel_context

user32 = ctypes.windll.user32

class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]

def get_primary_bounds() -> tuple[int, int, int, int]:
    """Return primary bounds in physical screen pixels, independent of caller DPI."""
    sm_cxscreen = 0
    sm_cyscreen = 1
    with physical_pixel_context(user32):
        width = int(user32.GetSystemMetrics(sm_cxscreen))
        height = int(user32.GetSystemMetrics(sm_cyscreen))
    return (0, 0, width, height)

def lock_cursor_to_primary() -> bool:
    """Clips the mouse cursor strictly to the primary physical monitor."""
    left, top, right, bottom = get_primary_bounds()
    rect = RECT(left, top, right, bottom)
    with physical_pixel_context(user32):
        return bool(user32.ClipCursor(ctypes.byref(rect)))

def unlock_cursor() -> bool:
    """Releases mouse cursor clipping."""
    return bool(user32.ClipCursor(None))


class MouseCursorGuard:
    """Background daemon ensuring mouse cursor remains constrained to primary monitor while active."""
    def __init__(self, interval: float = 0.5):
        self.interval = max(0.1, float(interval))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._enabled = False

    def start(self) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"enabled": True, "state": "running"}
            self._stop.clear()
            lock_cursor_to_primary()
            self._enabled = True
            self._thread = threading.Thread(target=self._run, name="mouse-cursor-guard", daemon=True)
            self._thread.start()
            return {"enabled": True, "state": "running"}

    def stop(self) -> dict:
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        with self._lock:
            unlock_cursor()
            self._enabled = False
            self._thread = None
            return {"enabled": False, "state": "off"}

    def status(self) -> dict:
        with self._lock:
            return {"enabled": self._enabled, "state": "running" if self._enabled else "off"}

    def _run(self):
        while not self._stop.wait(self.interval):
            # Windows may reset ClipCursor on full-screen transitions, alt-tab or task switches;
            # re-applying ensures user cursor never escapes to the virtual display.
            try:
                lock_cursor_to_primary()
            except Exception:
                pass


__all__ = ["MouseCursorGuard", "get_primary_bounds", "lock_cursor_to_primary", "unlock_cursor"]
