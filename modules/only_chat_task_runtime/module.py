"""Dedicated bounded IPC endpoint for Only Chat task-session requests.

The existing AI debug-display v1 pipe remains unchanged. This module routes a
separate protocol to the manager-owned task-session runtime and never creates a
target backend or allowlist by itself.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

from modules.only_chat_debug_display.module import NamedPipeServer
from core.runtime_packet import MAX_PACKET_BYTES

API_VERSION = 1
MODULE_ID = "only_chat_task_runtime"
VERSION = "0.1.7"
ACTIONS = ("status",)
PIPE_PATH = r"\\.\pipe\OnlyChatAiTaskRuntime.v2"
MAX_REQUEST_BYTES = 64 * 1024
MAX_REPLY_BYTES = 6 * 1024 * 1024
APP_PATH_REGISTRY = (Path.home() / "Desktop" / "gdrive" / "My Drive" /
                     "Codex App Registry" / "APP_PATH_REGISTRY.json")
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _normalized_executable_path(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return os.path.normcase(os.path.abspath(value)).casefold()
    except (OSError, TypeError, ValueError):
        return None


def registered_only_chat_exe_path(registry_path=APP_PATH_REGISTRY):
    """Read Only Chat's exact installed Windows image path from the shared registry."""
    try:
        data = json.loads(Path(registry_path).read_text(encoding="utf-8"))
        apps = data.get("apps") if isinstance(data, dict) else None
        record = next((item for item in apps if isinstance(item, dict)
                       and item.get("id") == "only-chat-windows"), None)
        windows = record.get("windows") if isinstance(record, dict) else None
        installed = windows.get("installed") if isinstance(windows, dict) else None
        if (not isinstance(installed, str) or not installed.strip()
                or not Path(installed).is_absolute()):
            return None
        installed_path = Path(installed)
        # The shared registry stores this app's install directory. Resolve the
        # executable name from the same authoritative app record.
        if installed_path.is_dir():
            app_name = record.get("name")
            if not isinstance(app_name, str) or not app_name.strip():
                return None
            installed_path = installed_path / f"{app_name}.exe"
        return installed_path if installed_path.is_file() else None
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def process_image_matches(pid, expected_path, api=None):
    """Allow only the exact registry-resolved Only Chat executable as a pipe client."""
    if os.name != "nt" or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    expected = _normalized_executable_path(str(expected_path))
    if expected is None:
        return False
    kernel = api or ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        capacity = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(capacity.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(capacity)):
            return False
        return _normalized_executable_path(buffer.value) == expected
    except (AttributeError, OSError, TypeError, ValueError):
        return False
    finally:
        kernel.CloseHandle(handle)


class OnlyChatTaskRuntimeModule:
    def __init__(self, server_factory=None, expected_client_path=None,
                 registry_path=APP_PATH_REGISTRY,
                 process_image_checker=process_image_matches):
        self._server_factory = server_factory
        self._registry_path = Path(registry_path)
        self._expected_client_path_override = (
            Path(expected_client_path) if expected_client_path is not None else None)
        self._process_image_checker = process_image_checker
        self._runtime = None
        self._server = None
        self._lock = threading.RLock()
        self._initialized = False
        self._startup_error = None
        self._authenticated_request_count = 0
        self._last_authenticated_request_unix_ms = None

    def _expected_client_path(self):
        if self._expected_client_path_override is not None:
            path = self._expected_client_path_override
            return path if path.is_file() else None
        return registered_only_chat_exe_path(self._registry_path)

    def _client_identity_matches(self, pid):
        expected = self._expected_client_path()
        if expected is None:
            return False
        try:
            return self._process_image_checker(pid, expected) is True
        except Exception:
            return False

    def init(self):
        with self._lock:
            if self._initialized:
                raise RuntimeError("Only Chat task-session endpoint already initialized")
            self._initialized = True
            # Hidden checks must not bind a process-global endpoint.
            if "--self-check" in sys.argv:
                self._startup_error = "BROKER_NOT_STARTED_IN_SELF_CHECK"

    def bind_runtime(self, runtime):
        if not callable(getattr(runtime, "handle_request", None)):
            raise TypeError("A task-session runtime with handle_request is required")
        with self._lock:
            if not self._initialized:
                raise RuntimeError("Only Chat task-session endpoint is not initialized")
            if "--self-check" in sys.argv:
                raise RuntimeError("Hidden self-check cannot bind the task-session endpoint")
            if self._runtime is not None or self._server is not None:
                raise RuntimeError("Only Chat task-session runtime is already bound")
            self._runtime = runtime
            self._server = (self._server_factory(self._handle_request)
                            if self._server_factory else self._make_default_server())
            try:
                self._server.start()
                self._startup_error = None
            except Exception as exc:
                self._startup_error = f"{type(exc).__name__}: {exc}"
                try:
                    self._server.stop()
                except Exception as stop_exc:
                    self._startup_error += f"; STOP_UNVERIFIED:{type(stop_exc).__name__}"
                else:
                    self._server = None
                    self._runtime = None

    def _make_default_server(self):
        return NamedPipeServer(
            self._handle_request, pipe_path=PIPE_PATH,
            max_request_bytes=MAX_REQUEST_BYTES,
            max_reply_bytes=MAX_REPLY_BYTES,
            client_authenticator=self._client_identity_matches,
            rejected_handler=lambda request: self._failure(
                request, "CLIENT_UNAUTHORIZED"),
            authenticated_handler=self._handle_authenticated_request,
        )

    def _handle_authenticated_request(self, request, client_pid):
        """Record and label only requests accepted by the exact-image pipe gate."""
        if (isinstance(client_pid, bool) or not isinstance(client_pid, int)
                or client_pid <= 0):
            return self._failure(request, "CLIENT_UNAUTHORIZED")
        with self._lock:
            self._authenticated_request_count += 1
            self._last_authenticated_request_unix_ms = int(time.time() * 1000)
        result = self._handle_request(request)
        result = dict(result)
        result["client_identity_verified"] = True
        return result

    def _handle_request(self, request):
        runtime = self._runtime
        if runtime is None:
            return self._failure(request, "RUNTIME_NOT_BOUND")
        try:
            result = runtime.handle_request(request)
        except Exception:
            # Never expose exception strings or process paths over the pipe.
            return self._failure(request, "RUNTIME_REQUEST_FAILED")
        if not isinstance(result, dict):
            return self._failure(request, "RUNTIME_REPLY_INVALID")
        packet = result.get("packet")
        if packet is not None:
            if not isinstance(packet, bytes) or not 0 < len(packet) <= MAX_PACKET_BYTES:
                return self._failure(request, "OBSERVATION_PACKET_INVALID")
            result = dict(result)
            result.pop("packet", None)
            result["packet_base64"] = base64.b64encode(packet).decode("ascii")
        return result

    @staticmethod
    def _failure(request, code):
        request_id = request.get("request_id") if isinstance(request, dict) else None
        return {"protocol_version": 2, "request_id": request_id,
                "session_id": None, "state": "failed", "error_code": code}

    def refresh(self):
        with self._lock:
            runtime = self._runtime
            server = self._server
            state = runtime.refresh() if runtime is not None else {}
            expected_client_path = self._expected_client_path()
            ready = bool(server and server.ready)
            error_code = self._startup_error
            if error_code is None and runtime is None:
                error_code = "RUNTIME_NOT_BOUND"
            if error_code is None and not ready:
                error_code = "BROKER_UNAVAILABLE"
            if (error_code is None and runtime is not None
                    and state.get("backend_available") is not True):
                error_code = "ISOLATED_BACKEND_UNAVAILABLE"
            if (error_code is None and runtime is not None
                    and state.get("target_count", 0) == 0):
                error_code = "TARGET_ALLOWLIST_EMPTY"
            output_lease_required = state.get("output_lease_required") is True
            if (error_code is None and output_lease_required
                    and state.get("output_lease_bound") is not True):
                error_code = "OUTPUT_LEASE_COORDINATOR_UNAVAILABLE"
            if (error_code is None and state.get("readiness_error") is not None):
                error_code = state["readiness_error"]
            return {
                "pipe_path": PIPE_PATH,
                "pipe_ready": ready,
                "client_identity_ready": expected_client_path is not None,
                "client_identity_policy": "registry_exact_image_path",
                "authenticated_request_count": self._authenticated_request_count,
                "last_authenticated_request_unix_ms": (
                    self._last_authenticated_request_unix_ms),
                "runtime_bound": runtime is not None,
                "backend_available": state.get("backend_available") is True,
                "target_count": state.get("target_count", 0),
                "output_lease_required": output_lease_required,
                "output_lease_bound": state.get("output_lease_bound") is True,
                "output_lease_ready": state.get("output_lease_ready") is True,
                "readiness_error": state.get("readiness_error"),
                "target_policies": state.get("target_policies", []),
                "active_session_id": state.get("active_session_id"),
                "lease_acquisition": "fail_closed",
                "error_code": (error_code or
                               (None if runtime is not None else "RUNTIME_NOT_BOUND")),
            }

    def action(self, name, _payload):
        if name != "status":
            raise ValueError("Only Chat task-session endpoint exports status only")
        return self.refresh()

    def snapshot(self):
        with self._lock:
            if self._runtime is not None or self._server is not None:
                raise RuntimeError("Task-session IPC binding requires a manager restart to reload")
            return {"runtime_bound": False}

    def restore(self, state):
        if state not in (None, {"runtime_bound": False}):
            raise ValueError("Task-session IPC endpoint state cannot be restored")

    def reload_blocked(self):
        with self._lock:
            return bool(self._server and self._server.active_clients)

    def dispose(self):
        with self._lock:
            server = self._server
            if server is not None:
                server.stop()
            self._server = None
            self._runtime = None
            self._initialized = False


def create():
    return OnlyChatTaskRuntimeModule()

