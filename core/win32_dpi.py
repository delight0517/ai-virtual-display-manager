"""Scoped Win32 DPI-awareness helpers for reliable physical-pixel geometry."""
from __future__ import annotations

import ctypes
from contextlib import contextmanager
from ctypes import wintypes

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)


@contextmanager
def physical_pixel_context(user32):
    """Temporarily use Per-Monitor-V2 coordinates and always restore the caller context."""
    setter = getattr(user32, "SetThreadDpiAwarenessContext", None)
    if setter is None:
        raise RuntimeError("Per-Monitor DPI context is unavailable; window coordinates are unverified")
    setter.argtypes = [wintypes.HANDLE]
    setter.restype = wintypes.HANDLE
    previous = setter(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    if not previous:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        if not setter(previous):
            raise ctypes.WinError(ctypes.get_last_error())
