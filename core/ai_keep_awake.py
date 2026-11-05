"""AI Keep-Awake / Away Mode Controller.
Allows continuous background execution for AI workloads even when displays sleep or monitors turn off.
"""
from __future__ import annotations

import ctypes
import threading

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040

kernel32 = ctypes.windll.kernel32

class AIKeepAwakeGuard:
    def __init__(self):
        self._enabled = False
        self._lock = threading.RLock()

    def start(self) -> bool:
        with self._lock:
            # Tell Windows that system execution is required and away mode is supported
            # Monitors can safely turn off, but CPU / network / AI processes will never sleep.
            flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
            res = kernel32.SetThreadExecutionState(flags)
            self._enabled = bool(res != 0)
            return self._enabled

    def stop(self) -> bool:
        with self._lock:
            # Revert to standard continuous power management
            res = kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            self._enabled = False
            return bool(res != 0)

    def is_active(self) -> bool:
        with self._lock:
            return self._enabled


_instance: AIKeepAwakeGuard | None = None

def get_keep_awake_guard() -> AIKeepAwakeGuard:
    global _instance
    if _instance is None:
        _instance = AIKeepAwakeGuard()
    return _instance


__all__ = ["AIKeepAwakeGuard", "get_keep_awake_guard"]
