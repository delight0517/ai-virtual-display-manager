"""Read-only WINDOWPLACEMENT conversion; caller must already use PMv2 DPI.

The result is monitor evidence, not authorization to move or close a window.
Callers must join it to fresh, verified display topology before classifying it.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
MONITOR_DEFAULTTONULL = 0


class _Rect(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class _MonitorInfoExW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", _Rect),
                ("rcWork", _Rect), ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32)]


def _valid_rect(value):
    return (isinstance(value, (list, tuple)) and len(value) == 4
            and all(isinstance(v, int) and not isinstance(v, bool)
                    and -(2 ** 31) <= v < 2 ** 31 for v in value)
            and value[0] < value[2] and value[1] < value[3])


def _coordinates(rect):
    return [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)]


def resolve_restore_bounds(user32, hwnd, bounds):
    """Return verified screen-space restore geometry, or None on uncertainty.

    Ordinary top-level WINDOWPLACEMENT rectangles use workspace coordinates;
    WS_EX_TOOLWINDOW rectangles already use screen coordinates. Do not use a
    nearest/primary monitor fallback when the original monitor is unavailable.
    """
    if not isinstance(hwnd, int) or isinstance(hwnd, bool) or hwnd <= 0:
        return None
    if not _valid_rect(bounds):
        return None
    try:
        get_style = getattr(user32, "GetWindowLongPtrW", None)
        if get_style is None:
            if ctypes.sizeof(ctypes.c_void_p) != 4:
                return None
            get_style = user32.GetWindowLongW
            get_style.restype = wintypes.LONG
        else:
            get_style.restype = ctypes.c_ssize_t
        get_style.argtypes = [wintypes.HWND, ctypes.c_int]

        # A zero exstyle is valid. Clear and inspect this API's LastError so
        # denied/invalid HWND reads never masquerade as ordinary windows.
        ctypes.set_last_error(0)
        style = int(get_style(wintypes.HWND(hwnd), GWL_EXSTYLE))
        if style == 0 and ctypes.get_last_error() != 0:
            return None

        from_window = user32.MonitorFromWindow
        from_window.argtypes = [wintypes.HWND, wintypes.DWORD]
        from_window.restype = wintypes.HANDLE
        monitor = from_window(wintypes.HWND(hwnd), MONITOR_DEFAULTTONULL)
        if not monitor:
            return None
        get_info = user32.GetMonitorInfoW
        get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MonitorInfoExW)]
        get_info.restype = wintypes.BOOL
        info = _MonitorInfoExW()
        info.cbSize = ctypes.sizeof(info)
        if not get_info(monitor, ctypes.byref(info)):
            return None
        monitor_bounds, work = _coordinates(info.rcMonitor), _coordinates(info.rcWork)
        if not _valid_rect(monitor_bounds) or not _valid_rect(work):
            return None
        if not (monitor_bounds[0] <= work[0] < work[2] <= monitor_bounds[2]
                and monitor_bounds[1] <= work[1] < work[3] <= monitor_bounds[3]):
            return None
        name = info.szDevice
        if not name or not name.strip():
            return None
        tool_window = bool(style & WS_EX_TOOLWINDOW)
        dx = 0 if tool_window else work[0] - monitor_bounds[0]
        dy = 0 if tool_window else work[1] - monitor_bounds[1]
        result = [bounds[0] + dx, bounds[1] + dy,
                  bounds[2] + dx, bounds[3] + dy]
        if not _valid_rect(result):
            return None
        if not (result[0] < monitor_bounds[2] and result[2] > monitor_bounds[0]
                and result[1] < monitor_bounds[3] and result[3] > monitor_bounds[1]):
            return None
        return {"bounds": result, "monitor_name": name,
                "monitor_bounds": monitor_bounds, "tool_window": tool_window}
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        return None
