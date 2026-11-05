"""Private WinSta0 desktop diagnostic, isolated from the interactive input desktop."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from core.private_capture_codes import PRIVATE_DESKTOP_CAPTURE_DIAGNOSTIC_CODES
from core.png_capture import (MAX_CAPTURE_PIXELS, MAX_PNG_BYTES, PNG_SIGNATURE,
                              encode_bgra_png, require_contentful_bgra_capture,
                              sampled_bgra_color_count)
from core.win32_dpi import physical_pixel_context

API_VERSION = 1
MODULE_ID = "private_desktop"
VERSION = "0.1.21"
ACTIONS = ("status", "probe", "start_diagnostic_session", "observe_session",
           "invoke_diagnostic_button", "stop_session")

DESKTOP_CREATEWINDOW = 0x0002
DESKTOP_ENUMERATE = 0x0040
DESKTOP_READOBJECTS = 0x0001
DESKTOP_WRITEOBJECTS = 0x0080
DESKTOP_ACCESS = (DESKTOP_CREATEWINDOW | DESKTOP_ENUMERATE |
                  DESKTOP_READOBJECTS | DESKTOP_WRITEOBJECTS)
UOI_NAME = 2
WM_SETTEXT = 0x000C
WM_CLOSE = 0x0010
SMTO_ABORTIFHUNG = 0x0002
PW_RENDERFULLCONTENT = 0x00000002
CREATE_SUSPENDED = 0x00000004
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
PROBE_WINDOW_PREFIX = "AIQA_PRIVATE_DESKTOP_"
ACK_WINDOW_TITLE = "AIQA_PRIVATE_DESKTOP_ACK"

class PrivateDesktopCaptureError(RuntimeError):
    """Capture failure exposing only a stable, non-sensitive stage code."""
    def __init__(self, diagnostic_code):
        if diagnostic_code not in PRIVATE_DESKTOP_CAPTURE_DIAGNOSTIC_CODES:
            raise ValueError("Unknown private-desktop capture diagnostic code")
        super().__init__(diagnostic_code)
        self.diagnostic_code = diagnostic_code


class _Rect(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class _StartupInfo(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
                ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
                ("hStdError", wintypes.HANDLE)]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class _RgbQuad(ctypes.Structure):
    _fields_ = [("rgbBlue", ctypes.c_ubyte), ("rgbGreen", ctypes.c_ubyte),
                ("rgbRed", ctypes.c_ubyte), ("rgbReserved", ctypes.c_ubyte)]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", _RgbQuad * 1)]


class _Win32:
    """Typed Win32 calls used by this feature only."""

    CALLBACK = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    DESKTOP_CALLBACK = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.LPWSTR, wintypes.LPARAM)

    def __init__(self):
        if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
            raise OSError("Private desktop support is Windows-only")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u, g, k = self.user32, self.gdi32, self.kernel32
        u.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
                                     wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        u.CreateDesktopW.restype = wintypes.HANDLE
        u.CloseDesktop.argtypes = [wintypes.HANDLE]
        u.CloseDesktop.restype = wintypes.BOOL
        u.GetThreadDesktop.argtypes = [wintypes.DWORD]
        u.GetThreadDesktop.restype = wintypes.HANDLE
        u.SetThreadDesktop.argtypes = [wintypes.HANDLE]
        u.SetThreadDesktop.restype = wintypes.BOOL
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowTextW.restype = ctypes.c_int
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetClassNameW.restype = ctypes.c_int
        u.EnumDesktopWindows.argtypes = [wintypes.HANDLE, self.CALLBACK, wintypes.LPARAM]
        u.EnumDesktopWindows.restype = wintypes.BOOL
        u.EnumWindows.argtypes = [self.CALLBACK, wintypes.LPARAM]
        u.EnumWindows.restype = wintypes.BOOL
        u.EnumDesktopsW.argtypes = [wintypes.HANDLE, self.DESKTOP_CALLBACK, wintypes.LPARAM]
        u.EnumDesktopsW.restype = wintypes.BOOL
        u.GetProcessWindowStation.restype = wintypes.HANDLE
        u.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        u.OpenInputDesktop.restype = wintypes.HANDLE
        u.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, wintypes.INT, ctypes.c_void_p,
                                                wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        u.GetUserObjectInformationW.restype = wintypes.BOOL
        u.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                          wintypes.LPCWSTR, wintypes.UINT, wintypes.UINT,
                                          ctypes.POINTER(ctypes.c_size_t)]
        u.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.PostMessageW.restype = wintypes.BOOL
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_Rect)]
        u.GetWindowRect.restype = wintypes.BOOL
        u.GetDC.argtypes = [wintypes.HWND]
        u.GetDC.restype = wintypes.HDC
        u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        u.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
        u.PrintWindow.restype = wintypes.BOOL
        g.CreateCompatibleDC.argtypes = [wintypes.HDC]
        g.CreateCompatibleDC.restype = wintypes.HDC
        g.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(_BitmapInfo),
                                       wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                       wintypes.HANDLE, wintypes.DWORD]
        g.CreateDIBSection.restype = wintypes.HBITMAP
        g.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        g.CreateCompatibleBitmap.restype = wintypes.HBITMAP
        g.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        g.SelectObject.restype = wintypes.HGDIOBJ
        g.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        g.GetPixel.restype = wintypes.COLORREF
        g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        g.DeleteDC.argtypes = [wintypes.HDC]
        k.GetCurrentThreadId.restype = wintypes.DWORD
        k.GetProcessId.argtypes = [wintypes.HANDLE]
        k.GetProcessId.restype = wintypes.DWORD
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                 wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
        k.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE,
                                     ctypes.POINTER(wintypes.BOOL)]
        k.IsProcessInJob.restype = wintypes.BOOL
        k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.TerminateJobObject.restype = wintypes.BOOL
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                              wintypes.DWORD]
        k.SetInformationJobObject.restype = wintypes.BOOL
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.AssignProcessToJobObject.restype = wintypes.BOOL
        k.ResumeThread.argtypes = [wintypes.HANDLE]
        k.ResumeThread.restype = wintypes.DWORD
        k.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
                                     ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p,
                                     wintypes.LPCWSTR, ctypes.POINTER(_StartupInfo),
                                     ctypes.POINTER(_ProcessInformation)]
        k.CreateProcessW.restype = wintypes.BOOL
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.GetExitCodeProcess.restype = wintypes.BOOL
        k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.TerminateProcess.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL

    def object_name(self, handle):
        buf = ctypes.create_unicode_buffer(260)
        needed = wintypes.DWORD()
        if not self.user32.GetUserObjectInformationW(handle, UOI_NAME, buf,
                                                      ctypes.sizeof(buf), ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        return buf.value

    def input_desktop_name(self, manager_thread_id=None):
        ctypes.set_last_error(0)
        handle = self.user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
        if not handle:
            error = ctypes.get_last_error()
            if error != 5:
                raise ctypes.WinError(error)
            # Read-only fallback: the foreground window's owning thread is on
            # the current input desktop. Accept this name only when it matches
            # this manager thread's assigned desktop; a secure/other desktop
            # therefore stays visible as a mismatch and callers fail closed.
            name, same_as_manager, _manager_handle = self._foreground_input_desktop(
                manager_thread_id)
            return name if same_as_manager else "unknown"
        try:
            return self.object_name(handle)
        finally:
            self.kernel32.CloseHandle(handle)

    def _foreground_input_desktop(self, manager_thread_id=None):
        """Return foreground/manager desktop names and the borrowed manager handle."""
        thread_id = (self.kernel32.GetCurrentThreadId() if manager_thread_id is None
                     else int(manager_thread_id))
        if thread_id <= 0:
            raise OSError(5, "Manager desktop thread identity is unavailable")
        manager_desktop = self.user32.GetThreadDesktop(thread_id)
        if not manager_desktop:
            raise ctypes.WinError(ctypes.get_last_error())
        manager_name = self.object_name(manager_desktop)
        for _attempt in range(2):
            before = int(self.user32.GetForegroundWindow() or 0)
            if not before:
                raise OSError(5, "Foreground desktop identity is unavailable")
            foreground_pid = wintypes.DWORD()
            foreground_thread_id = self.user32.GetWindowThreadProcessId(
                before, ctypes.byref(foreground_pid))
            if not foreground_thread_id:
                raise ctypes.WinError(ctypes.get_last_error())
            foreground_desktop = self.user32.GetThreadDesktop(foreground_thread_id)
            if not foreground_desktop:
                raise ctypes.WinError(ctypes.get_last_error())
            foreground_name = self.object_name(foreground_desktop)
            after = int(self.user32.GetForegroundWindow() or 0)
            if before == after:
                return foreground_name, foreground_name == manager_name, manager_desktop
        raise OSError(5, "Foreground desktop changed during read-only verification")

    def desktop_exists(self, name: str):
        names = []

        @self.DESKTOP_CALLBACK
        def collect(candidate, _param):
            names.append(candidate)
            return True

        station = self.user32.GetProcessWindowStation()
        if not station or not self.user32.EnumDesktopsW(station, collect, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        return name in names

    def window_is_on_input_desktop(self, hwnd: int, manager_thread_id=None):
        ctypes.set_last_error(0)
        desktop = self.user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
        borrowed = False
        if not desktop:
            error = ctypes.get_last_error()
            if error != 5:
                raise ctypes.WinError(error)
            input_name, same_as_manager, desktop = self._foreground_input_desktop(
                manager_thread_id)
            if not same_as_manager:
                raise OSError(5, "Input desktop differs from the manager thread desktop")
            borrowed = True
        windows = []
        @self.CALLBACK
        def collect(candidate, _param):
            windows.append(int(candidate))
            return True
        try:
            if not self.user32.EnumDesktopWindows(desktop, collect, 0):
                raise ctypes.WinError(ctypes.get_last_error())
            if borrowed:
                after_name, same_after, _ = self._foreground_input_desktop(
                    manager_thread_id)
                if not same_after or after_name != input_name:
                    raise OSError(5, "Input desktop changed during window enumeration")
            return int(hwnd) in windows
        finally:
            if not borrowed:
                self.kernel32.CloseHandle(desktop)

    def probe_command(self, token: str):
        if getattr(sys, "frozen", False):
            executable = sys.executable
            args = ["--private-desktop-probe", token]
        else:
            executable = sys.executable
            args = [str(Path(__file__).resolve().parents[2] / "app.py"),
                    "--private-desktop-probe", token]
        return executable, args

    def target_command(self, descriptor):
        """Return only the immutable descriptor's already-verified launch tuple."""
        executable = str(Path(descriptor.executable_path).resolve(strict=True))
        return executable, list(descriptor.arguments)

    def uia_worker_command(self, hwnd: int, report_path: str):
        if getattr(sys, "frozen", False):
            executable = sys.executable
            args = ["--private-desktop-uia-worker", str(hwnd), report_path]
        else:
            executable = sys.executable
            args = [str(Path(__file__).resolve().parents[2] / "app.py"),
                    "--private-desktop-uia-worker", str(hwnd), report_path]
        return executable, args

    def uia_action_worker_command(self, hwnd: int, descriptor_path: str,
                                  report_path: str):
        if getattr(sys, "frozen", False):
            executable = sys.executable
            args = ["--private-desktop-uia-action-worker", str(hwnd),
                    descriptor_path, report_path]
        else:
            executable = sys.executable
            args = [str(Path(__file__).resolve().parents[2] / "app.py"),
                    "--private-desktop-uia-action-worker", str(hwnd),
                    descriptor_path, report_path]
        return executable, args

    def capture_nonblank(self, hwnd):
        rect = _Rect()
        if not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise ctypes.WinError(ctypes.get_last_error())
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width < 80 or height < 60 or width > 2500 or height > 2000:
            return False, 0
        screen = self.user32.GetDC(None)
        if not screen:
            raise ctypes.WinError(ctypes.get_last_error())
        memory = bitmap = previous = None
        try:
            memory = self.gdi32.CreateCompatibleDC(screen)
            bitmap = self.gdi32.CreateCompatibleBitmap(screen, width, height)
            if not memory or not bitmap:
                raise ctypes.WinError(ctypes.get_last_error())
            previous = self.gdi32.SelectObject(memory, bitmap)
            if not self.user32.PrintWindow(hwnd, memory, PW_RENDERFULLCONTENT):
                return False, 0
            colors = set()
            for y in range(0, height, max(1, height // 12)):
                for x in range(0, width, max(1, width // 12)):
                    color = int(self.gdi32.GetPixel(memory, x, y))
                    if color != 0xFFFFFFFF:
                        colors.add(color)
            return len(colors) >= 3, len(colors)
        finally:
            if memory and previous:
                self.gdi32.SelectObject(memory, previous)
            if bitmap:
                self.gdi32.DeleteObject(bitmap)
            if memory:
                self.gdi32.DeleteDC(memory)
            self.user32.ReleaseDC(None, screen)

    def capture_png(self, hwnd):
        """Capture one exact HWND into bounded in-memory PNG bytes."""
        try:
            with physical_pixel_context(self.user32):
                rect = _Rect()
                if not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    raise PrivateDesktopCaptureError("capture_window_rect_failed")
        except PrivateDesktopCaptureError:
            raise
        except Exception as exc:
            raise PrivateDesktopCaptureError("capture_dpi_context_failed") from exc
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width < 1 or height < 1 or width * height > MAX_CAPTURE_PIXELS:
            raise PrivateDesktopCaptureError("capture_dimensions_invalid")
        screen = self.user32.GetDC(None)
        if not screen:
            raise PrivateDesktopCaptureError("capture_screen_dc_failed")
        memory = bitmap = previous = None
        try:
            memory = self.gdi32.CreateCompatibleDC(screen)
            if not memory:
                raise PrivateDesktopCaptureError("capture_compatible_dc_failed")
            info = _BitmapInfo()
            info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
            info.bmiHeader.biWidth = width
            info.bmiHeader.biHeight = -height  # top-down BGRA rows
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            info.bmiHeader.biCompression = 0  # BI_RGB
            info.bmiHeader.biSizeImage = width * height * 4
            bits = ctypes.c_void_p()
            bitmap = self.gdi32.CreateDIBSection(screen, ctypes.byref(info), 0,
                                                  ctypes.byref(bits), None, 0)
            if not bitmap or not bits.value:
                raise PrivateDesktopCaptureError("capture_bitmap_create_failed")
            previous = self.gdi32.SelectObject(memory, bitmap)
            if not previous:
                raise PrivateDesktopCaptureError("capture_bitmap_select_failed")
            ctypes.memset(bits.value, 0, info.bmiHeader.biSizeImage)
            if not self.user32.PrintWindow(hwnd, memory, PW_RENDERFULLCONTENT):
                raise PrivateDesktopCaptureError("capture_print_window_failed")
            bgra = ctypes.string_at(bits.value, info.bmiHeader.biSizeImage)
            try:
                color_count = require_contentful_bgra_capture(bgra, width, height)
            except Exception as exc:
                raise PrivateDesktopCaptureError("capture_content_invalid") from exc
            try:
                png = encode_bgra_png(bgra, width, height)
            except Exception as exc:
                raise PrivateDesktopCaptureError("capture_png_encode_failed") from exc
            if (not png.startswith(PNG_SIGNATURE) or len(png) > MAX_PNG_BYTES):
                raise PrivateDesktopCaptureError("capture_png_validation_failed")
            return png, width, height, color_count
        finally:
            if memory and previous:
                self.gdi32.SelectObject(memory, previous)
            if bitmap:
                self.gdi32.DeleteObject(bitmap)
            if memory:
                self.gdi32.DeleteDC(memory)
            self.user32.ReleaseDC(None, screen)


def run_private_desktop_probe(api=None) -> dict:
    """Launch one owned test window off the input desktop, capture and tear it down."""
    try:
        api = api or _Win32()
    except Exception as exc:
        return {"passed": False, "error_code": "PRIVATE_DESKTOP_UNAVAILABLE",
                "detail": f"{type(exc).__name__}: {exc}"[:180]}
    u, k = api.user32, api.kernel32
    # Pin the caller's host desktop identity: CreateDesktopW may affect its
    # caller, so fallback checks must not infer the host from a probe worker.
    host_thread_id = int(k.GetCurrentThreadId())
    token = uuid.uuid4().hex[:12]
    title = f"{PROBE_WINDOW_PREFIX}{token}"
    desktop_name = f"AIVDDQA_{token}"
    input_before = None
    desktop = job = None
    process = _ProcessInformation()
    uia_process = _ProcessInformation()
    hwnd = None
    worker_thread = None
    cleanup = {"process_stopped": False, "desktop_thread_restored": False,
               "desktop_handle_closed": False, "job_handle_closed": False}
    result = {"passed": False, "desktop": desktop_name, "display_enabled": False,
              "window_message_roundtrip": False, "capture_nonblank": False,
              "capture_png_valid": False, "capture_png_nonblank": False,
              "uia_control": {"passed": False, "window_found": False,
                              "button_found": False, "button_invoked": False,
                              "status_verified": False},
              "host_foreground_unchanged": False,
              "window_absent_from_input_desktop": False, "input_desktop_unchanged": False}
    uia_report_path = None

    def probe_thread():
        nonlocal desktop, job, hwnd, worker_thread, input_before
        original = u.GetThreadDesktop(k.GetCurrentThreadId())
        worker_thread = threading.current_thread()
        try:
            input_before = api.input_desktop_name(host_thread_id)
            desktop = u.CreateDesktopW(desktop_name, None, None, 0, DESKTOP_ACCESS, None)
            if not desktop:
                raise ctypes.WinError(ctypes.get_last_error())
            startup = _StartupInfo()
            startup.cb = ctypes.sizeof(startup)
            startup.lpDesktop = f"WinSta0\\{desktop_name}"
            executable, args = api.probe_command(token)
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline([executable, *args]))
            job = k.CreateJobObjectW(None, None)
            if not job:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = _ExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                             ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not k.CreateProcessW(executable, command, None, None, False, CREATE_SUSPENDED,
                                    None, str(Path(executable).resolve().parent),
                                    ctypes.byref(startup), ctypes.byref(process)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not k.AssignProcessToJobObject(job, process.hProcess):
                raise ctypes.WinError(ctypes.get_last_error())
            if k.ResumeThread(process.hThread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())

            windows = []

            @api.CALLBACK
            def collect(candidate, _param):
                buf = ctypes.create_unicode_buffer(260)
                u.GetWindowTextW(candidate, buf, len(buf))
                pid = wintypes.DWORD()
                thread_id = u.GetWindowThreadProcessId(candidate, ctypes.byref(pid))
                if pid.value == process.dwProcessId and buf.value == title:
                    windows.append((int(candidate), int(thread_id)))
                return True

            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not windows:
                ctypes.set_last_error(0)
                ok = u.EnumDesktopWindows(desktop, collect, 0)
                if not ok and ctypes.get_last_error():
                    raise ctypes.WinError(ctypes.get_last_error())
                time.sleep(0.05)
            if not windows:
                raise TimeoutError("Owned private-desktop test window did not appear")
            hwnd, _thread_id = windows[0]
            result["desktop_name_verified"] = api.object_name(desktop) == desktop_name

            result["window_absent_from_input_desktop"] = not (
                api.window_is_on_input_desktop(hwnd, host_thread_id))
            if not result["window_absent_from_input_desktop"]:
                raise RuntimeError("Private probe window is on the host input desktop")

            sent = ctypes.c_size_t()
            if not u.SendMessageTimeoutW(hwnd, WM_SETTEXT, 0, ACK_WINDOW_TITLE,
                                         SMTO_ABORTIFHUNG, 1000, ctypes.byref(sent)):
                raise ctypes.WinError(ctypes.get_last_error())
            readback = ctypes.create_unicode_buffer(260)
            u.GetWindowTextW(hwnd, readback, len(readback))
            result["window_message_roundtrip"] = readback.value == ACK_WINDOW_TITLE

            report_fd, report_name = tempfile.mkstemp(prefix="AIVDDQA_UIA_", suffix=".json")
            os.close(report_fd)
            uia_report_path = Path(report_name)
            uia_executable, uia_args = api.uia_worker_command(hwnd, str(uia_report_path))
            foreground_before = int(u.GetForegroundWindow() or 0)
            uia_startup = _StartupInfo()
            uia_startup.cb = ctypes.sizeof(uia_startup)
            uia_startup.lpDesktop = f"WinSta0\\{desktop_name}"
            uia_command = ctypes.create_unicode_buffer(
                subprocess.list2cmdline([uia_executable, *uia_args]))
            if not k.CreateProcessW(uia_executable, uia_command, None, None, False,
                                    CREATE_SUSPENDED, None,
                                    str(Path(uia_executable).resolve().parent),
                                    ctypes.byref(uia_startup), ctypes.byref(uia_process)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not k.AssignProcessToJobObject(job, uia_process.hProcess):
                raise ctypes.WinError(ctypes.get_last_error())
            if k.ResumeThread(uia_process.hThread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            uia_wait = k.WaitForSingleObject(uia_process.hProcess, 7000)
            if uia_wait != WAIT_OBJECT_0:
                k.TerminateProcess(uia_process.hProcess, 0xDEAD)
                k.WaitForSingleObject(uia_process.hProcess, 2000)
                raise TimeoutError("Private-desktop UIA worker did not finish within its bound")
            uia_exit = wintypes.DWORD()
            if not k.GetExitCodeProcess(uia_process.hProcess, ctypes.byref(uia_exit)):
                raise ctypes.WinError(ctypes.get_last_error())
            if uia_report_path.exists():
                uia_report = json.loads(uia_report_path.read_text(encoding="utf-8"))
                expected_uia_keys = ("passed", "window_found", "button_found",
                                     "button_invoked", "status_verified")
                result["uia_control"] = {key: uia_report.get(key) is True
                                          for key in expected_uia_keys}
                result["uia_control"]["control_summary"] = uia_report.get("control_summary", [])
                result["uia_control"]["error_code"] = uia_report.get("error_code")
            result["uia_control"]["worker_exit_code"] = int(uia_exit.value)
            result["uia_control"]["passed"] = (
                result["uia_control"].get("passed") is True and uia_exit.value == 0)
            result["host_foreground_unchanged"] = (
                foreground_before != 0 and int(u.GetForegroundWindow() or 0) == foreground_before)
            if not result["uia_control"]["passed"]:
                raise RuntimeError("Private-desktop semantic UIA action did not verify")
            if not result["host_foreground_unchanged"]:
                raise RuntimeError("Private-desktop UIA changed the host foreground window")

            png, capture_width, capture_height, unique_colors = api.capture_png(hwnd)
            nonblank = unique_colors >= 3
            result["capture_nonblank"] = nonblank
            result["capture_png_nonblank"] = nonblank
            result["capture_unique_sample_colors"] = int(unique_colors)
            result["capture_png_valid"] = png.startswith(PNG_SIGNATURE) and bool(png)
            result["capture_png"] = {"width": int(capture_width), "height": int(capture_height),
                                     "byte_count": len(png),
                                     "sha256": hashlib.sha256(png).hexdigest()}
            result["input_desktop_before"] = input_before
        except Exception as exc:
            result["error_code"] = f"{type(exc).__name__}: {exc}"[:180]
        finally:
            if uia_process.hProcess:
                uia_wait = k.WaitForSingleObject(uia_process.hProcess, 0)
                if uia_wait != WAIT_OBJECT_0:
                    k.TerminateProcess(uia_process.hProcess, 0xDEAD)
                    uia_wait = k.WaitForSingleObject(uia_process.hProcess, 2000)
                cleanup["uia_worker_stopped"] = uia_wait == WAIT_OBJECT_0
                if uia_process.hThread:
                    k.CloseHandle(uia_process.hThread)
                    uia_process.hThread = None
                k.CloseHandle(uia_process.hProcess)
                uia_process.hProcess = None
            else:
                cleanup["uia_worker_stopped"] = False
            if process.hProcess:
                if hwnd:
                    u.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                wait = k.WaitForSingleObject(process.hProcess, 3000)
                cleanup["process_stopped"] = wait == WAIT_OBJECT_0
                if wait != WAIT_OBJECT_0 and job:
                    # Job close below is the primary kill boundary; this is only
                    # an exact-handle fallback for a hung self-authored probe.
                    k.TerminateProcess(process.hProcess, 0xDEAD)
                    cleanup["process_stopped"] = k.WaitForSingleObject(process.hProcess, 2000) == WAIT_OBJECT_0
                exit_code = wintypes.DWORD()
                if k.GetExitCodeProcess(process.hProcess, ctypes.byref(exit_code)):
                    cleanup["process_exit_code"] = int(exit_code.value)
                    cleanup["process_stopped"] = cleanup["process_stopped"] and exit_code.value == 0
                k.CloseHandle(process.hThread)
                k.CloseHandle(process.hProcess)
                process.hThread = process.hProcess = None
            if job:
                cleanup["job_handle_closed"] = bool(k.CloseHandle(job))
                job = None
            if original and u.SetThreadDesktop(original):
                cleanup["desktop_thread_restored"] = True
            elif original:
                result["cleanup_error"] = f"SetThreadDesktop: {ctypes.get_last_error()}"
            if desktop and cleanup["desktop_thread_restored"] and u.CloseDesktop(desktop):
                cleanup["desktop_handle_closed"] = True
                desktop = None
            elif desktop:
                result["cleanup_error"] = result.get("cleanup_error") or f"CloseDesktop: {ctypes.get_last_error()}"
            _mark_uncreated_probe_resources(cleanup, process, uia_process, job, desktop)

    thread = threading.Thread(target=probe_thread, name="private-desktop-probe", daemon=True)
    thread.start()
    thread.join(12000)
    if thread.is_alive():
        result["error_code"] = "PRIVATE_DESKTOP_PROBE_TIMEOUT"
        return result

    try:
        input_after = api.input_desktop_name(host_thread_id)
        result["input_desktop_after"] = input_after
        result["input_desktop_unchanged"] = input_before == input_after
    except Exception as exc:
        result["input_desktop_unchanged"] = False
        result["cleanup_error"] = f"input desktop readback: {type(exc).__name__}: {exc}"[:180]
    if desktop and cleanup["desktop_thread_restored"] and u.CloseDesktop(desktop):
        cleanup["desktop_handle_closed"] = True
        desktop = None
    _verify_desktop_removal(cleanup, api, desktop_name, result)
    result["cleanup"] = cleanup
    result["passed"] = all((result.get("desktop_name_verified"),
                            result.get("window_message_roundtrip"),
                            result.get("uia_control", {}).get("passed"),
                            result.get("host_foreground_unchanged"),
                            result.get("capture_nonblank"),
                            result.get("capture_png_valid"),
                            result.get("capture_png_nonblank"),
                            result.get("window_absent_from_input_desktop"),
                            result.get("input_desktop_unchanged"),
                            cleanup["process_stopped"], cleanup["desktop_thread_restored"],
                            cleanup["desktop_handle_closed"], cleanup["desktop_removed"],
                            cleanup["job_handle_closed"], cleanup["uia_worker_stopped"]))
    if uia_report_path is not None:
        try:
            uia_report_path.unlink(missing_ok=True)
            result["uia_report_removed"] = not uia_report_path.exists()
            result["passed"] = result["passed"] and result["uia_report_removed"]
        except OSError:
            result["uia_report_removed"] = False
            result["passed"] = False
    return result


def _verify_desktop_removal(cleanup: dict, api, desktop_name: str, result: dict) -> None:
    """Always report cleanup failure as evidence instead of raising during finalization."""
    cleanup["desktop_removed"] = False
    if cleanup.get("desktop_handle_closed") is not True:
        return
    deadline = time.monotonic() + 2.0
    last_error = None
    while True:
        try:
            if not api.desktop_exists(desktop_name):
                cleanup["desktop_removed"] = True
                return
        except Exception as exc:
            last_error = exc
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    if last_error is not None:
        result["cleanup_error"] = f"desktop list readback: {type(last_error).__name__}: {last_error}"[:180]


def _mark_uncreated_probe_resources(cleanup: dict, process, uia_process,
                                    job, desktop) -> None:
    """Treat resources that were never created as requiring no cleanup."""
    if not process.hProcess:
        cleanup["process_stopped"] = True
    if not uia_process.hProcess:
        cleanup["uia_worker_stopped"] = True
    if not job:
        cleanup["job_handle_closed"] = True
    if not desktop:
        cleanup["desktop_handle_closed"] = True


class PrivateDesktopModule:
    def __init__(self, probe_runner=None):
        self._probe_runner = probe_runner or run_private_desktop_probe
        self._lock = threading.RLock()
        self._initialized = False
        self._busy = False
        self._last_probe = None
        self._session = None

    def init(self):
        with self._lock:
            if self._initialized:
                raise RuntimeError("Private desktop module is already initialized")
            self._initialized = True

    def snapshot(self):
        with self._lock:
            if self._busy or (self._session is not None and self._session.active):
                raise RuntimeError("Private desktop probe is active; reload is deferred")
            return {"last_probe": self._last_probe}

    def restore(self, state):
        if state and state.get("last_probe") is not None:
            self._last_probe = dict(state["last_probe"])

    def refresh(self):
        with self._lock:
            probe = ("passed" if self._last_probe and self._last_probe.get("passed") else
                     "failed" if self._last_probe else "not_run")
            session = self._session
        return {"backend": "windows_private_desktop_probe",
                "supported": os.name == "nt" and hasattr(ctypes, "WinDLL"), "probe": probe,
                "diagnostic_session": "active" if session and session.active else "inactive",
                "consumer_launch": "not_implemented", "consumer_lease": "fail_closed"}

    def action(self, name, payload):
        if name == "status":
            return self.refresh()
        if name == "start_diagnostic_session":
            if payload.get("confirmed") is not True:
                raise ValueError("Starting the isolated diagnostic session requires explicit confirmation")
            with self._lock:
                if self._busy or (self._session is not None and self._session.active):
                    raise RuntimeError("A private-desktop operation/session is already active")
                self._busy = True
            try:
                from modules.private_desktop.session import DiagnosticSession
                session = DiagnosticSession(_Win32(), _StartupInfo,
                                            _ProcessInformation, _ExtendedLimitInformation)
                with self._lock:
                    self._session = session
                return {"started": True, "session_id": session.session_id,
                        "desktop": session.desktop_name, "target": "manager-diagnostic-only",
                        "window_absent_from_input_desktop":
                            session._state.get("window_absent_from_input_desktop") is True,
                        "input_desktop_unchanged": session._state.get("input_desktop_unchanged") is True,
                        "display_enabled": False}
            finally:
                with self._lock:
                    self._busy = False
        if name in {"observe_session", "invoke_diagnostic_button", "stop_session"}:
            session_id = payload.get("session_id")
            with self._lock:
                session = self._session
                if self._busy:
                    raise RuntimeError("A private-desktop session action is already active")
                if session is None or session_id != session.session_id:
                    raise ValueError("Session identity is missing or stale")
                self._busy = True
            try:
                operation = {"observe_session": "observe",
                             "invoke_diagnostic_button": "invoke",
                             "stop_session": "stop"}[name]
                result = session.request(operation)
                if name == "stop_session" and result.get("stopped") is True:
                    with self._lock:
                        self._session = None
                return result
            finally:
                with self._lock:
                    self._busy = False
        if name != "probe":
            raise ValueError("Unsupported private desktop action")
        if payload.get("confirmed") is not True:
            raise ValueError("Private desktop probe requires explicit confirmation")
        with self._lock:
            if self._busy:
                raise RuntimeError("Private desktop probe is already active")
            self._busy = True
        try:
            result = self._probe_runner()
            with self._lock:
                self._last_probe = dict(result)
            return result
        finally:
            with self._lock:
                self._busy = False

    def dispose(self):
        with self._lock:
            if self._busy:
                raise RuntimeError("Private desktop probe is active; stop is deferred")
            session = self._session
        if session is not None:
            receipt = session.close()
            if receipt.get("stopped") is not True:
                raise RuntimeError("Private-desktop session cleanup was not verified")
        with self._lock:
            self._session = None
            self._initialized = False


def create():
    return PrivateDesktopModule()
