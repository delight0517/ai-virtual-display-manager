"""Minimal MTT VDD named-pipe client. Only documented commands are allowed."""
from __future__ import annotations

import ctypes
import time

PIPE_NAME = r"\\.\pipe\MTTVirtualDisplayPipe"
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
PIPE_NOWAIT = 0x00000001
_ALLOWED = {"PING"}


def validate_command(command: str) -> str:
    value = command.strip().upper()
    if value in _ALLOWED:
        return value
    parts = value.split()
    if len(parts) == 2 and parts[0] == "SETDISPLAYCOUNT" and parts[1].isdigit():
        count = int(parts[1])
        if 0 <= count <= 8:
            return f"SETDISPLAYCOUNT {count}"
    raise ValueError("Only PING and SETDISPLAYCOUNT 0..8 are supported")


def send(command: str, timeout_ms: int = 900):
    command = validate_command(command)
    if not hasattr(ctypes, "WinDLL"):
        raise OSError("The MTT VDD pipe client is available on Windows only")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong,
                                     ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.SetNamedPipeHandleState.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong),
                                                  ctypes.c_void_p, ctypes.c_void_p]
    kernel32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                                   ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
    kernel32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                                  ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.CreateFileW(PIPE_NAME, GENERIC_READ | GENERIC_WRITE, 0, None,
                                  OPEN_EXISTING, 0, None)
    if handle == INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(), "MTT VDD control pipe is unavailable")
    try:
        mode = ctypes.c_ulong(PIPE_NOWAIT)
        if not kernel32.SetNamedPipeHandleState(handle, ctypes.byref(mode), None, None):
            raise OSError(ctypes.get_last_error(), "Could not configure VDD pipe timeout")
        payload = command.encode("utf-16le")
        written = ctypes.c_ulong()
        if not kernel32.WriteFile(handle, payload, len(payload), ctypes.byref(written), None):
            raise OSError(ctypes.get_last_error(), "Could not send command to VDD")
        if written.value != len(payload):
            raise OSError("Incomplete VDD command write")
        deadline = time.monotonic() + timeout_ms / 1000
        chunks = []
        while time.monotonic() < deadline:
            buf = ctypes.create_string_buffer(4096)
            read = ctypes.c_ulong()
            if not kernel32.ReadFile(handle, buf, len(buf), ctypes.byref(read), None):
                error = ctypes.get_last_error()
                if error == 232:  # No response data yet; keep the bounded deadline.
                    time.sleep(0.02)
                    continue
                if error in (109, 233):  # Driver disconnects after reading the command.
                    break
                raise OSError(error, "Could not read VDD response")
            if not read.value:
                break
            chunks.append(buf.raw[:read.value])
        if not chunks:
            return "DELIVERED_NO_REPLY"
        return b"".join(chunks).decode("utf-8", errors="replace").rstrip("\x00\r\n")
    finally:
        kernel32.CloseHandle(handle)
