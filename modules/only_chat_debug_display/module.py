"""Only Chat v1 debug-display broker transport, closed to leases without isolation."""
from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import threading
import time
from ctypes import wintypes

API_VERSION = 1
MODULE_ID = "only_chat_debug_display"
VERSION = "0.1.3"
ACTIONS = ("status",)
PIPE_PATH = r"\\.\pipe\OnlyChatAiDebugDisplay.v1"
MAX_MESSAGE_BYTES = 4096
IDLE_TIMEOUT_SECONDS = 300
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
HOST_INPUT_DESKTOP_INTERRUPTED = "HOST_INPUT_DESKTOP_INTERRUPTED"
HOST_INPUT_DESKTOP_UNKNOWN = "HOST_INPUT_DESKTOP_UNKNOWN"

DESKTOP_READOBJECTS = 0x0001
UOI_NAME = 2


def read_input_desktop_state(api=None) -> str:
    """Return default/other/unknown without interacting with desktop windows."""
    if os.name != "nt" and api is None:
        return "unknown"
    api = api or ctypes.WinDLL("user32", use_last_error=True)
    api.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenInputDesktop.restype = wintypes.HANDLE
    api.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    api.GetUserObjectInformationW.restype = wintypes.BOOL
    api.CloseDesktop.argtypes = [wintypes.HANDLE]
    api.CloseDesktop.restype = wintypes.BOOL

    desktop = api.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if not desktop:
        return "unknown"
    try:
        needed = wintypes.DWORD()
        api.GetUserObjectInformationW(desktop, UOI_NAME, None, 0, ctypes.byref(needed))
        if not needed.value or needed.value > 4096:
            return "unknown"
        char_count = (needed.value + ctypes.sizeof(ctypes.c_wchar) - 1) // ctypes.sizeof(ctypes.c_wchar)
        name_buffer = ctypes.create_unicode_buffer(char_count)
        if not api.GetUserObjectInformationW(desktop, UOI_NAME, name_buffer,
                                             ctypes.sizeof(name_buffer), ctypes.byref(needed)):
            return "unknown"
        return "default" if name_buffer.value.casefold() == "default" else "other"
    except (AttributeError, OSError, TypeError, ValueError):
        return "unknown"
    finally:
        api.CloseDesktop(desktop)

PIPE_ACCESS_DUPLEX = 0x00000003
FILE_FLAG_OVERLAPPED = 0x40000000
PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
ERROR_IO_PENDING = 997
ERROR_PIPE_CONNECTED = 535
ERROR_BROKEN_PIPE = 109
ERROR_NO_DATA = 232
ERROR_OPERATION_ABORTED = 995
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258


class _Overlapped(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE)]


class NamedPipeServer:
    """Single-client local pipe server with bounded, cancellable overlapped I/O."""

    def __init__(self, handler, api=None, *, pipe_path=None,
                 max_request_bytes=MAX_MESSAGE_BYTES,
                 max_reply_bytes=MAX_MESSAGE_BYTES,
                 client_authenticator=None, rejected_handler=None,
                 authenticated_handler=None):
        pipe_path = PIPE_PATH if pipe_path is None else pipe_path
        if (not isinstance(pipe_path, str) or not pipe_path.startswith("\\\\.\\pipe\\")
                or len(pipe_path) > 240):
            raise ValueError("A bounded local Windows named-pipe path is required")
        for name, value in (("max_request_bytes", max_request_bytes),
                            ("max_reply_bytes", max_reply_bytes)):
            if (isinstance(value, bool) or not isinstance(value, int)
                    or not 1 <= value <= 8 * 1024 * 1024):
                raise ValueError(f"{name} must be between 1 byte and 8 MiB")
        self.handler = handler
        if ((client_authenticator is None) != (rejected_handler is None)
                or (client_authenticator is not None and not callable(client_authenticator))
                or (rejected_handler is not None and not callable(rejected_handler))
                or (authenticated_handler is not None and not callable(authenticated_handler))
                or (authenticated_handler is not None and client_authenticator is None)):
            raise ValueError("Client authentication requires both an authenticator and a rejection handler")
        self.client_authenticator = client_authenticator
        self.rejected_handler = rejected_handler
        self.authenticated_handler = authenticated_handler
        self.pipe_path = pipe_path
        self.max_request_bytes = max_request_bytes
        self.max_reply_bytes = max_reply_bytes
        self.api = api or (ctypes.WinDLL("kernel32", use_last_error=True)
                           if os.name == "nt" else None)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._handle = None
        self._active_clients = 0
        self._startup_error: str | None = None
        if self.api is not None:
            self._bind_api()

    def _bind_api(self):
        self.api.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, ctypes.c_void_p]
        self.api.CreateNamedPipeW.restype = wintypes.HANDLE
        self.api.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Overlapped)]
        self.api.ConnectNamedPipe.restype = wintypes.BOOL
        self.api.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_Overlapped)]
        self.api.ReadFile.restype = wintypes.BOOL
        self.api.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_Overlapped)]
        self.api.WriteFile.restype = wintypes.BOOL
        self.api.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
            wintypes.BOOL, wintypes.LPCWSTR]
        self.api.CreateEventW.restype = wintypes.HANDLE
        self.api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.api.WaitForSingleObject.restype = wintypes.DWORD
        self.api.GetOverlappedResult.argtypes = [wintypes.HANDLE,
            ctypes.POINTER(_Overlapped), ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
        self.api.GetOverlappedResult.restype = wintypes.BOOL
        self.api.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Overlapped)]
        self.api.CancelIoEx.restype = wintypes.BOOL
        self.api.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
        self.api.DisconnectNamedPipe.restype = wintypes.BOOL
        self.api.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        self.api.FlushFileBuffers.restype = wintypes.BOOL
        self.api.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenThread.restype = wintypes.HANDLE
        self.api.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
        self.api.CancelSynchronousIo.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        if self.client_authenticator is not None:
            self.api.GetNamedPipeClientProcessId.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
            self.api.GetNamedPipeClientProcessId.restype = wintypes.BOOL

    @property
    def active_clients(self):
        with self._lock:
            return self._active_clients

    @property
    def ready(self):
        return self.api is not None and self._ready.is_set() and self._startup_error is None

    def start(self):
        if self.api is None:
            raise OSError("Only Chat broker is supported on Windows")
        if self._thread and self._thread.is_alive():
            raise RuntimeError("Only Chat broker is already running")
        self._stop.clear()
        self._ready.clear()
        self._startup_error = None
        self._thread = threading.Thread(target=self._serve, name="only-chat-display-broker", daemon=True)
        self._thread.start()
        if not self._ready.wait(2.0):
            self.stop()
            raise TimeoutError("Only Chat named pipe did not become ready")
        if self._startup_error:
            self.stop()
            raise OSError(self._startup_error)

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            # FlushFileBuffers waits for the client to consume the response. If
            # shutdown races a stalled client, cancel only this owned server thread.
            thread_id = thread.native_id
            if thread_id:
                thread_handle = self.api.OpenThread(0x0001, False, int(thread_id))
                if thread_handle:
                    self.api.CancelSynchronousIo(thread_handle)
                    self.api.CloseHandle(thread_handle)
            thread.join(timeout=2.0)
            if thread.is_alive():
                raise TimeoutError("Only Chat named pipe did not stop cleanly")
        self._thread = None
        self._ready.clear()

    def _serve(self):
        try:
            while not self._stop.is_set():
                handle = self.api.CreateNamedPipeW(self.pipe_path,
                    PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED,
                    PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                    1, self.max_request_bytes + 1, self.max_reply_bytes + 1, 0, None)
                invalid = ctypes.c_void_p(-1).value
                if handle in (None, invalid):
                    raise ctypes.WinError(ctypes.get_last_error())
                with self._lock:
                    self._handle = handle
                self._ready.set()
                try:
                    if not self._connect(handle):
                        continue
                    with self._lock:
                        self._active_clients += 1
                    try:
                        request = self._read_request(handle)
                        if self.client_authenticator is not None:
                            client_pid = wintypes.ULONG()
                            try:
                                identified = bool(self.api.GetNamedPipeClientProcessId(
                                    handle, ctypes.byref(client_pid))) and client_pid.value > 0
                            except (AttributeError, OSError, TypeError, ValueError):
                                identified = False
                            reply = self._dispatch_request(
                                request, int(client_pid.value) if identified else None)
                        else:
                            reply = self.handler(request)
                        encoded = (json.dumps(reply, separators=(",", ":"),
                                             ensure_ascii=True) + "\n").encode("utf-8")
                        if len(encoded) > self.max_reply_bytes + 1:
                            raise ValueError("Local broker reply exceeds the protocol limit")
                        self._write_reply(handle, encoded)
                        # Do not disconnect before the stream client has received
                        # the newline-framed reply. The client reads immediately.
                        if not self.api.FlushFileBuffers(handle):
                            error = ctypes.get_last_error()
                            if not (self._stop.is_set() and error == ERROR_OPERATION_ABORTED):
                                raise ctypes.WinError(error)
                    except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
                        pass
                    finally:
                        with self._lock:
                            self._active_clients -= 1
                finally:
                    self.api.DisconnectNamedPipe(handle)
                    self.api.CloseHandle(handle)
                    with self._lock:
                        if self._handle == handle:
                            self._handle = None
        except Exception as exc:
            self._startup_error = f"{type(exc).__name__}: {exc}"
            self._ready.set()

    def _new_overlapped(self):
        event = self.api.CreateEventW(None, True, False, None)
        if not event:
            raise ctypes.WinError(ctypes.get_last_error())
        return _Overlapped(0, 0, 0, 0, event)

    def _wait_io(self, handle, operation, buffer, size):
        overlapped = self._new_overlapped()
        transferred = wintypes.DWORD()
        pending = False
        try:
            if operation == "read":
                ok = self.api.ReadFile(handle, buffer, size, ctypes.byref(transferred), ctypes.byref(overlapped))
            elif operation == "write":
                ok = self.api.WriteFile(handle, buffer, size, ctypes.byref(transferred), ctypes.byref(overlapped))
            else:
                ok = self.api.ConnectNamedPipe(handle, ctypes.byref(overlapped))
            if ok:
                return int(transferred.value)
            error = ctypes.get_last_error()
            if operation == "connect" and error == ERROR_PIPE_CONNECTED:
                return 0
            if error != ERROR_IO_PENDING:
                if operation == "read" and error in (ERROR_BROKEN_PIPE, ERROR_NO_DATA):
                    return 0
                raise ctypes.WinError(error)
            pending = True
            deadline = None if operation == "connect" else time.monotonic() + 2.0
            while not self._stop.is_set():
                wait = self.api.WaitForSingleObject(overlapped.hEvent, 50)
                if wait == WAIT_OBJECT_0:
                    if not self.api.GetOverlappedResult(handle, ctypes.byref(overlapped),
                                                        ctypes.byref(transferred), False):
                        error = ctypes.get_last_error()
                        if error in (ERROR_BROKEN_PIPE, ERROR_NO_DATA, ERROR_OPERATION_ABORTED):
                            return 0
                        raise ctypes.WinError(error)
                    pending = False
                    return int(transferred.value)
                if wait != WAIT_TIMEOUT:
                    raise ctypes.WinError(ctypes.get_last_error())
                if deadline is not None and time.monotonic() >= deadline:
                    return None
            return None
        finally:
            if pending:
                self.api.CancelIoEx(handle, ctypes.byref(overlapped))
                self.api.GetOverlappedResult(handle, ctypes.byref(overlapped),
                                             ctypes.byref(transferred), True)
            self.api.CloseHandle(overlapped.hEvent)

    def _connect(self, handle):
        return self._wait_io(handle, "connect", None, 0) is not None

    def _read_request(self, handle):
        data = bytearray()
        while len(data) <= self.max_request_bytes:
            buffer = ctypes.create_string_buffer(
                min(512, self.max_request_bytes + 1 - len(data)))
            count = self._wait_io(handle, "read", buffer, len(buffer) - 1)
            if count is None or count == 0:
                raise OSError("Local broker request timed out or pipe closed")
            data.extend(buffer.raw[:count])
            if b"\n" in data:
                if data[-1:] != b"\n" or data.count(b"\n") != 1:
                    raise ValueError("Local broker accepts one newline-framed request")
                if len(data) - 1 > self.max_request_bytes:
                    raise ValueError("Local broker request exceeds the protocol limit")
                return json.loads(data[:-1].decode("utf-8", errors="strict"))
        raise ValueError("Local broker request exceeds the protocol limit")

    def _write_reply(self, handle, payload):
        buffer = ctypes.create_string_buffer(payload)
        offset = 0
        while offset < len(payload):
            count = self._wait_io(handle, "write", ctypes.byref(buffer, offset),
                                  len(payload) - offset)
            if count is None or count == 0:
                raise OSError("Only Chat broker reply timed out")
            offset += count

    def _dispatch_request(self, request, client_pid=None):
        """Dispatch with optional OS-authenticated client PID context."""
        if self.client_authenticator is None:
            return self.handler(request)
        try:
            authorized = (isinstance(client_pid, int) and not isinstance(client_pid, bool)
                          and client_pid > 0 and self.client_authenticator(client_pid) is True)
        except Exception:
            authorized = False
        if not authorized:
            return self.rejected_handler(request)
        if self.authenticated_handler is not None:
            return self.authenticated_handler(request, client_pid)
        return self.handler(request)


class OnlyChatDebugDisplayModule:
    def __init__(self, server_factory=None, input_desktop_reader=None):
        self._server_factory = server_factory or NamedPipeServer
        self._input_desktop_reader = input_desktop_reader or read_input_desktop_state
        self._server = None
        self._lock = threading.RLock()
        self._initialized = False
        self._startup_error: str | None = None

    def init(self):
        with self._lock:
            if self._initialized:
                raise RuntimeError("Only Chat debug-display broker already initialized")
            if "--self-check" not in sys.argv:
                self._server = self._server_factory(self._handle_request)
                try:
                    self._server.start()
                except Exception as exc:
                    # Keep the manager usable while reporting an unavailable broker.
                    self._startup_error = f"{type(exc).__name__}: {exc}"
                    try:
                        self._server.stop()
                    except Exception:
                        pass
                    self._server = None
            self._initialized = True

    def snapshot(self):
        with self._lock:
            if self._server:
                if self._server.active_clients:
                    raise RuntimeError("Only Chat broker request is active; reload is deferred")
                if self._server.ready:
                    raise RuntimeError("The process-global pipe listener requires a manager restart to reload")
            return {"active_run_id": None}

    def restore(self, state):
        if state and state.get("active_run_id") is not None:
            raise RuntimeError("Only Chat display leases cannot be restored without an isolation backend")

    def refresh(self):
        server_ready = bool(self._server and self._server.ready)
        desktop_state = self._read_desktop_state()
        desktop_error = (HOST_INPUT_DESKTOP_INTERRUPTED if desktop_state == "other" else
                         HOST_INPUT_DESKTOP_UNKNOWN if desktop_state != "default" else None)
        return {"pipe_path": PIPE_PATH, "connected": server_ready,
                "state": "failed", "display_enabled": False,
                "idle_seconds": 0, "idle_timeout_seconds": IDLE_TIMEOUT_SECONDS,
                "input_desktop_state": desktop_state,
                "error_code": (desktop_error if desktop_error else
                               "ISOLATED_DESKTOP_UNAVAILABLE" if server_ready else
                               getattr(self, "_startup_error", None) or
                               ("BROKER_NOT_STARTED_IN_SELF_CHECK" if "--self-check" in sys.argv
                                else "BROKER_UNAVAILABLE")),
                "isolation_backend": "unavailable", "lease_acquisition": "fail_closed"}

    def action(self, name, _payload):
        if name != "status":
            raise ValueError("Only Chat broker exports status only")
        return self.refresh()

    @staticmethod
    def _reply(request, state="failed", run_id=None, error_code="ISOLATED_DESKTOP_UNAVAILABLE"):
        return {"version": 1,
                "request_id": request.get("request_id", "00000000-0000-4000-8000-000000000000"),
                "state": state, "run_id": run_id, "idle_seconds": 0,
                "idle_timeout_seconds": IDLE_TIMEOUT_SECONDS,
                "display_enabled": False, "error_code": error_code}

    def _handle_request(self, request):
        if not isinstance(request, dict):
            return self._reply({})
        action = request.get("action")
        request_id = request.get("request_id")
        if (type(request.get("version")) is not int or request.get("version") != 1
                or not isinstance(request_id, str)
                or not UUID_RE.fullmatch(request_id)):
            return self._reply(request, error_code="REQUEST_INVALID")
        expected = {"status": {"version", "request_id", "action"},
                    "acquire": {"version", "request_id", "action", "run_id", "confirmed"},
                    "heartbeat": {"version", "request_id", "action", "run_id"},
                    "release": {"version", "request_id", "action", "run_id"}}
        if action not in expected or set(request) != expected[action]:
            return self._reply(request, error_code="REQUEST_INVALID")
        if action == "status":
            return self._reply(request, error_code=self._current_error_code())
        run_id = request.get("run_id")
        if not isinstance(run_id, str) or not UUID_RE.fullmatch(run_id):
            return self._reply(request, error_code="RUN_ID_INVALID")
        if action == "acquire" and request.get("confirmed") is not True:
            return self._reply(request, error_code="CONFIRMATION_REQUIRED")
        if action == "acquire":
            # The host-attached MTT output is not an isolated desktop. Refuse to
            # start a lease until a supported private-desktop backend exists.
            return self._reply(request, error_code=self._current_error_code())
        return self._reply(request, run_id=run_id, error_code="NO_ACTIVE_LEASE")

    def _read_desktop_state(self):
        try:
            state = self._input_desktop_reader()
        except Exception:
            return "unknown"
        return state if state in {"default", "other"} else "unknown"

    def _current_error_code(self):
        desktop_state = self._read_desktop_state()
        if desktop_state == "other":
            return HOST_INPUT_DESKTOP_INTERRUPTED
        if desktop_state != "default":
            return HOST_INPUT_DESKTOP_UNKNOWN
        return "ISOLATED_DESKTOP_UNAVAILABLE"

    def dispose(self):
        with self._lock:
            if self._server:
                self._server.stop()
            self._server = None
            self._initialized = False


def create():
    return OnlyChatDebugDisplayModule()
