"""Per-Windows-session single-instance guard for the desktop manager."""
from __future__ import annotations

import ctypes

ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = r"Local\AI.VirtualDisplayManager"


class SingleInstanceGuard:
    def __init__(self, handle):
        self._handle = handle

    @classmethod
    def acquire(cls):
        if not hasattr(ctypes, "WinDLL"):
            raise OSError("This application is Windows-only")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.CreateMutexW(None, 0, MUTEX_NAME)
        if not handle:
            raise OSError(ctypes.get_last_error(), "Could not create the app instance mutex")
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return None
        return cls(handle)

    def close(self):
        if self._handle:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self._handle)
            self._handle = None
