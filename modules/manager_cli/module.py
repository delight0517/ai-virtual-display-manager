"""Authenticated, allowlisted local operator API for VDM's standalone CLI."""
from __future__ import annotations

import ctypes
import base64
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from ctypes import wintypes
from pathlib import Path

from modules.only_chat_debug_display.module import NamedPipeServer
from modules.only_chat_task_runtime.module import process_image_matches
from modules.virtual_output_pool.module import PROFILES
from core.runtime_packet import MAX_PACKET_BYTES

API_VERSION = 1
MODULE_ID = "manager_cli"
VERSION = "0.1.21"
SAFE_RUNTIME_ERROR_CODES = frozenset({"PHYSICAL_PRIMARY_UNVERIFIED"})
ACTIONS = ("status",)
PIPE_PATH = r"\\.\pipe\AIVDM_ManagerControl.v1"
CONTROL_PIPE_PATH = r"\\.\pipe\AIVDM_TaskControl.v1"
UI_CONTROL_PIPE_PATH = r"\\.\pipe\AIVDM_UiControl.v1"
PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 16_384
MAX_REPLY_BYTES = 6 * 1024 * 1024
MAX_VDD_SETTINGS_BYTES = 262_144
MAX_REQUESTS_PER_MINUTE = 120
MAX_REQUEST_IDS = 65_536
PLAN_TTL_SECONDS = 60
LOG_MAX_BYTES = 1_048_576
STARTUP_LOG_MAX_BYTES = 65_536
LOG_FIELDS = frozenset({"event", "timestamp", "app", "window_state", "output",
                        "result", "reason", "setting", "enabled", "topology",
                        "output_count", "bounds", "monitor_identity_refresh_state",
                        "monitor_identity_age_ms", "monitor_identity_cache_hit",
                        "display_count", "identified_display_count", "provider_error_type"})
EVENTS = frozenset({"window_observed", "restore_requested", "restore_result", "guard_state",
                    "guard_start_result", "topology_state", "manager_state", "display_settings_change"})
APP_KINDS = frozenset({"discord", "browser", "explorer", "focusfense", "other"})
WINDOW_STATES = frozenset({"normal", "minimized", "maximized", "hidden", "unknown"})
OUTPUT_KINDS = frozenset({"primary", "physical_secondary", "virtual", "offscreen", "unknown"})
RESULTS = frozenset({"started", "succeeded", "failed", "unavailable", "skipped", "closed", "unknown"})
REASONS = frozenset({"topology_unverified", "window_minimized", "identity_changed", "api_failed",
                     "permission_denied", "timeout", "no_window", "no_physical_output", "unknown",
                     "monitor_identity_refresh_pending", "monitor_identity_refresh_failed",
                     "topology_provider_exception", "display_path_query_failed",
                     "monitor_identity_unverified"})
MONITOR_REFRESH_STATES = frozenset({"fresh", "refreshing", "pending", "error", "unavailable", "timeout"})
PROVIDER_ERROR_TYPES = frozenset({"OSError", "TimeoutError", "RuntimeError", "ValueError", "Other"})
SETTINGS = frozenset({"physical_window_guard_enabled", "display_topology_changed"})
TOPOLOGIES = frozenset({"physical_and_virtual", "physical_only", "virtual_only", "unknown"})
PROFILE_NAMES = {"balanced": "1080p · 60 Hz", "high": "1440p · 60 Hz",
                 "ultra": "4K · 30 Hz"}
PLAN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
REQUEST_COMMANDS = frozenset({"status", "logs", "outputs.preview", "outputs.apply",
                              "outputs.return_preview", "outputs.return", "task.status",
                              "task.start", "task.observe", "task.stop", "screens", "windows",
                              "ui.show", "ui.hide", "ui.stop", "ui.status", "stardesk.status"})
SCREEN_FIELDS = ("display_name", "target_name", "x", "y", "width", "height",
                 "refresh_rate", "attached", "verified", "topology_verified", "virtual",
                 "primary", "cloned", "mirroring_driver", "monitor_identity_verified")


def _parse_vdd_settings_modes(raw):
    """Read declared VDD modes as configuration evidence, never live evidence."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_VDD_SETTINGS_BYTES:
        raise ValueError("OUTPUT_MODE_EVIDENCE_UNAVAILABLE")
    try:
        document = raw.decode("utf-8-sig", errors="strict")
    except UnicodeError:
        raise ValueError("OUTPUT_MODE_EVIDENCE_UNAVAILABLE") from None
    lowered = document.lower()
    if "\x00" in document or "<!doctype" in lowered or "<!entity" in lowered:
        raise ValueError("OUTPUT_MODE_EVIDENCE_UNAVAILABLE")
    try:
        root = ET.fromstring(document)
    except (ET.ParseError, UnicodeError, ValueError):
        raise ValueError("OUTPUT_MODE_EVIDENCE_UNAVAILABLE") from None
    if root.tag != "vdd_settings":
        raise ValueError("OUTPUT_MODE_EVIDENCE_UNAVAILABLE")

    def read_integer(node, tag, maximum):
        value = node.findtext(tag)
        try:
            parsed = int(value.strip(), 10)
        except (AttributeError, TypeError, ValueError):
            raise ValueError("OUTPUT_MODE_EVIDENCE_UNAVAILABLE") from None
        if not 1 <= parsed <= maximum:
            raise ValueError("OUTPUT_MODE_EVIDENCE_UNAVAILABLE")
        return parsed

    global_rates = [read_integer(node, ".", 1000)
                    for node in root.findall("./global/g_refresh_rate")]
    resolutions = root.findall("./resolutions/resolution")
    modes = set()
    for item in resolutions:
        width = read_integer(item, "width", 16_384)
        height = read_integer(item, "height", 16_384)
        rate_nodes = item.findall("refresh_rate")
        rates = [read_integer(node, ".", 1000) for node in rate_nodes] or global_rates
        modes.update((width, height, rate) for rate in rates)
    return [list(mode) for mode in sorted(modes)]


def _read_vdd_settings_mode_evidence():
    """Load a bounded local VDD settings file and bind its modes to its hash."""
    try:
        import winreg
    except ImportError:
        raise ValueError("OUTPUT_MODE_EVIDENCE_UNAVAILABLE") from None
    base_path = r"C:\VirtualDisplayDriver"
    try:
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\MikeTheTech\VirtualDisplayDriver") as key:
            configured, kind = winreg.QueryValueEx(key, "VDDPATH")
    except FileNotFoundError:
        pass
    except OSError:
        raise ValueError("OUTPUT_MODE_EVIDENCE_UNAVAILABLE") from None
    else:
        if kind != winreg.REG_SZ or not isinstance(configured, str) or not configured.strip():
            raise ValueError("OUTPUT_MODE_EVIDENCE_UNAVAILABLE")
        base_path = configured.strip()

    drive, _tail = os.path.splitdrive(base_path)
    if (len(drive) != 2 or drive[1] != ":" or not drive[0].isalpha()
            or not os.path.isabs(base_path) or base_path.startswith(("\\\\", "//"))):
        raise ValueError("OUTPUT_MODE_EVIDENCE_UNAVAILABLE")
    settings_path = Path(base_path) / "vdd_settings.xml"
    try:
        # Reject reparse points in every component so a configured path cannot
        # redirect this read to a network location or an unrelated file.
        current = Path(drive + os.sep)
        relative_parts = os.path.relpath(str(settings_path), drive + os.sep).split(os.sep)
        for part in relative_parts:
            if part in {"", "."}:
                continue
            current = current / part
            info = os.lstat(current)
            if getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("OUTPUT_MODE_EVIDENCE_UNAVAILABLE")
        info = os.stat(settings_path, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_VDD_SETTINGS_BYTES:
            raise ValueError("OUTPUT_MODE_EVIDENCE_UNAVAILABLE")
        raw = settings_path.read_bytes()
    except (OSError, ValueError):
        raise ValueError("OUTPUT_MODE_EVIDENCE_UNAVAILABLE") from None
    modes = _parse_vdd_settings_modes(raw)
    return {"modes": modes, "sha256": hashlib.sha256(raw).hexdigest()}


def _token_sid(token, kernel, security):
    needed = wintypes.DWORD()
    security.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
    if not 1 <= needed.value <= 65_536:
        return None
    data = ctypes.create_string_buffer(needed.value)
    if not security.GetTokenInformation(token, 1, data, needed.value, ctypes.byref(needed)):
        return None
    sid = ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p)).contents.value
    sid_text = wintypes.LPWSTR()
    security.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    security.ConvertSidToStringSidW.restype = wintypes.BOOL
    if not security.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
        return None
    try:
        return str(sid_text.value)
    finally:
        kernel.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))


def same_user_process(pid, *, api=None):
    """Accept only a peer owned by the current Windows user; never remote clients."""
    if os.name != "nt" and api is None:
        return False
    api = api or ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True) if os.name == "nt" else api
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.GetCurrentProcess.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.HANDLE)]
    security.OpenProcessToken.restype = wintypes.BOOL
    security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    security.GetTokenInformation.restype = wintypes.BOOL
    process = api.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        return False
    peer_token = wintypes.HANDLE()
    current_token = wintypes.HANDLE()
    try:
        if not security.OpenProcessToken(process, 0x0008, ctypes.byref(peer_token)):
            return False
        if not security.OpenProcessToken(api.GetCurrentProcess(), 0x0008, ctypes.byref(current_token)):
            return False
        peer_sid = _token_sid(peer_token, api, security)
        current_sid = _token_sid(current_token, api, security)
        return peer_sid is not None and peer_sid == current_sid
    except (AttributeError, OSError, TypeError, ValueError):
        return False
    finally:
        if peer_token:
            api.CloseHandle(peer_token)
        if current_token:
            api.CloseHandle(current_token)
        api.CloseHandle(process)


def _safe_log_event(row):
    if (not isinstance(row, dict) or not isinstance(row.get("event"), str)
            or row.get("event") not in EVENTS):
        return None
    timestamp = row.get("timestamp")
    if (not isinstance(timestamp, str) or not re.fullmatch(
            r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)", timestamp)):
        return None
    safe = {"event": row["event"], "timestamp": timestamp}
    for key in LOG_FIELDS:
        value = row.get(key)
        if key in {"event", "timestamp"} or key not in row:
            continue
        if key == "app" and isinstance(value, str) and value in APP_KINDS:
            safe[key] = value
        elif key == "window_state" and isinstance(value, str) and value in WINDOW_STATES:
            safe[key] = value
        elif key == "output" and isinstance(value, str) and value in OUTPUT_KINDS:
            safe[key] = value
        elif key == "result" and isinstance(value, str) and value in RESULTS:
            safe[key] = value
        elif key == "reason" and isinstance(value, str) and value in REASONS:
            safe[key] = value
        elif key == "setting" and isinstance(value, str) and value in SETTINGS:
            safe[key] = value
        elif key == "topology" and isinstance(value, str) and value in TOPOLOGIES:
            safe[key] = value
        elif key == "output_count" and type(value) is int and 0 <= value <= 16:
            safe[key] = value
        elif key == "bounds":
            if (isinstance(value, list) and len(value) == 4
                    and all(type(item) is int and abs(item) <= 1_000_000 for item in value)):
                safe[key] = value
        elif key == "monitor_identity_refresh_state" and value in MONITOR_REFRESH_STATES:
            safe[key] = value
        elif key == "monitor_identity_age_ms" and type(value) is int and 0 <= value <= 86_400_000:
            safe[key] = value
        elif key == "monitor_identity_cache_hit" and type(value) is bool:
            safe[key] = value
        elif key in {"display_count", "identified_display_count"} and type(value) is int and 0 <= value <= 16:
            safe[key] = value
        elif key == "provider_error_type" and value in PROVIDER_ERROR_TYPES:
            safe[key] = value
        elif key == "enabled":
            if type(value) is bool:
                safe[key] = value
    return safe


class ManagerCliModule:
    def __init__(self, server_factory=None, process_image_checker=process_image_matches,
                 user_process_checker=same_user_process, clock=None, id_factory=None,
                 log_path=None, configured_mode_reader=None):
        self._server_factory = server_factory or NamedPipeServer
        self._process_image_checker = process_image_checker
        self._user_process_checker = user_process_checker
        self._clock = clock or time.time
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._log_path = Path(log_path) if log_path is not None else None
        self._configured_mode_reader = configured_mode_reader or _read_vdd_settings_mode_evidence
        self._dispatcher = None
        self._server = None
        self._control_server = None
        self._ui_control_server = None
        self._startup_error = None
        self._initialized = False
        self._lock = threading.RLock()
        self._request_ids = set()
        self._request_times = []
        self._plans = {}
        self._accepting = True
        self._stardesk_reader = None

    def configured_output_mode_evidence(self):
        """Return validated VDD settings modes, explicitly as configuration evidence."""
        try:
            evidence = self._configured_mode_reader()
        except Exception:
            raise ValueError("OUTPUT_MODE_EVIDENCE_UNAVAILABLE") from None
        if not isinstance(evidence, dict):
            raise ValueError("OUTPUT_MODE_EVIDENCE_UNAVAILABLE")
        raw_modes = evidence.get("modes")
        digest = evidence.get("sha256")
        if (not isinstance(raw_modes, list) or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
            raise ValueError("OUTPUT_MODE_EVIDENCE_UNAVAILABLE")
        modes = set()
        for item in raw_modes:
            if (not isinstance(item, (list, tuple)) or len(item) != 3
                    or any(type(value) is not int or value <= 0 for value in item)):
                raise ValueError("OUTPUT_MODE_EVIDENCE_UNAVAILABLE")
            modes.add(tuple(item))
        if not modes:
            raise ValueError("OUTPUT_MODE_EVIDENCE_UNAVAILABLE")
        return {"modes": [list(mode) for mode in sorted(modes)],
                "source": "configured_vdd_settings", "sha256": digest}

    def init(self):
        with self._lock:
            if self._initialized:
                raise RuntimeError("Manager CLI module is already initialized")
            self._initialized = True

    def bind_dependencies(self, *, status_reader, topology_reader, pool_status,
                          pool_begin, pool_stop, task_action, window_reader, ui_action=None,
                          stardesk_reader=None):
        callbacks = (status_reader, topology_reader, pool_status, pool_begin, pool_stop,
                     task_action, window_reader)
        if any(not callable(item) for item in callbacks):
            raise TypeError("Manager CLI callbacks must be callable")
        with self._lock:
            if not self._initialized or self._server is not None:
                raise RuntimeError("Manager CLI module is not ready to bind")
            self._status_reader, self._topology_reader, self._pool_status = callbacks[:3]
            self._pool_begin, self._pool_stop = callbacks[3:5]
            self._task_action = callbacks[5]
            self._window_reader = callbacks[6]
            if ui_action is not None and not callable(ui_action):
                raise TypeError("UI callback must be callable")
            self._ui_action = ui_action
            if stardesk_reader is not None and not callable(stardesk_reader):
                raise TypeError("StarDesk status callback must be callable")
            self._stardesk_reader = stardesk_reader
            self._dispatcher = self._dispatch_command
            if "--self-check" in sys.argv:
                self._startup_error = "PIPE_NOT_STARTED_IN_SELF_CHECK"
                return
            self._start_listener()

    def _new_server(self, *, pipe_path=None, handler=None):
        if getattr(sys, "frozen", False):
            expected_client = Path(sys.executable).with_name("vdmctl.exe")
        else:
            # A Windows venv's python.exe may dispatch to its base executable.
            # Authenticate the image Windows actually reports, keeping the
            # exact-image check in source and test runs.
            expected_client = Path(getattr(sys, "_base_executable", None) or sys.executable)
        request_handler = handler or self._handle_request
        return self._server_factory(
            request_handler,
            pipe_path=pipe_path or PIPE_PATH,
            max_request_bytes=MAX_REQUEST_BYTES,
            max_reply_bytes=MAX_REPLY_BYTES,
            client_authenticator=lambda pid: (
                self._process_image_checker(pid, expected_client) is True
                and self._user_process_checker(pid) is True),
            rejected_handler=lambda _request: self._reply(
                None, False, error_code="CLIENT_UNAUTHORIZED"),
            authenticated_handler=lambda request, _pid: request_handler(request),
        )

    def _handle_control_request(self, request):
        if not isinstance(request, dict) or request.get("command") not in {"task.status", "task.stop"}:
            return self._reply(request.get("request_id") if isinstance(request, dict) else None,
                               False, error_code="CONTROL_COMMAND_UNSUPPORTED")
        return self._handle_request(request)

    def _handle_ui_control_request(self, request):
        if not isinstance(request, dict) or request.get("command") not in {
                "ui.status", "ui.show", "ui.hide", "ui.stop"}:
            return self._reply(request.get("request_id") if isinstance(request, dict) else None,
                               False, error_code="UI_CONTROL_COMMAND_UNSUPPORTED")
        return self._handle_request(request)

    def _start_listener(self):
        try:
            server = self._new_server()
            server.start()
            self._server = server
            control = self._new_server(pipe_path=CONTROL_PIPE_PATH, handler=self._handle_control_request)
            control.start()
            self._control_server = control
            ui_control = self._new_server(pipe_path=UI_CONTROL_PIPE_PATH,
                                          handler=self._handle_ui_control_request)
            ui_control.start()
            self._ui_control_server = ui_control
            self._startup_error = None
            self._accepting = True
        except Exception as exc:
            self._startup_error = f"PIPE_START_FAILED:{type(exc).__name__}"
            self._accepting = False
            self._server = locals().get("server")
            self._control_server = locals().get("control")
            self._ui_control_server = locals().get("ui_control")
            cleanup_failed = False
            for owned in (self._server, self._control_server, self._ui_control_server):
                if owned:
                    try:
                        owned.stop()
                    except Exception:
                        cleanup_failed = True
            if cleanup_failed:
                self._startup_error = "PIPE_START_FAILED:CLEANUP_UNVERIFIED"
            else:
                self._server = None
                self._control_server = None
                self._ui_control_server = None

    def pause_for_shutdown(self):
        """Stop new CLI work only when no request is currently executing."""
        with self._lock:
            servers = [s for s in (self._server, self._control_server, self._ui_control_server) if s]
            if any(s.active_clients for s in servers):
                return False
            self._accepting = False
        try:
            for server in servers:
                server.stop()
        except Exception:
            with self._lock:
                self._accepting = False
                self._startup_error = "PIPE_STOP_FAILED:CLEANUP_UNVERIFIED"
            raise
        with self._lock:
            self._server = None
            self._control_server = None
            self._ui_control_server = None
            self._startup_error = "PIPE_STOPPED_FOR_CLOSE"
            return True

    def resume_after_shutdown(self):
        with self._lock:
            if not self._initialized or self._dispatcher is None:
                return False
            if str(self._startup_error or "").endswith("CLEANUP_UNVERIFIED"):
                return False
            if (self._server and self._server.ready and self._control_server
                    and self._control_server.ready and self._ui_control_server
                    and self._ui_control_server.ready):
                self._accepting = True
                return True
            servers = [s for s in (self._server, self._control_server, self._ui_control_server) if s]
            if any(s.active_clients for s in servers):
                return False
            self._accepting = False
        for server in servers:
            server.stop()
        with self._lock:
            self._server = None
            self._control_server = None
            self._ui_control_server = None
            self._start_listener()
            return bool(self._server and self._server.ready and self._control_server
                        and self._control_server.ready and self._ui_control_server
                        and self._ui_control_server.ready)

    def _reply(self, request_id, ok, *, data=None, error_code=None):
        return {"protocol_version": PROTOCOL_VERSION, "request_id": request_id,
                "ok": bool(ok), "data": data, "error_code": error_code}

    def _handle_request(self, request):
        request_id = request.get("request_id") if isinstance(request, dict) else None
        with self._lock:
            if not self._accepting:
                return self._reply(request_id if isinstance(request_id, str) else None,
                                   False, error_code="MANAGER_SHUTTING_DOWN")
        if (not isinstance(request, dict)
                or set(request) != {"protocol_version", "request_id", "issued_at_ms", "command", "arguments"}
                or type(request.get("protocol_version")) is not int
                or request.get("protocol_version") != PROTOCOL_VERSION
                or not isinstance(request_id, str) or not re.fullmatch(
                    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                    request_id, re.I)
                or type(request.get("issued_at_ms")) is not int
                or abs(int(self._clock() * 1000) - request["issued_at_ms"]) > 60_000
                or not isinstance(request.get("command"), str)
                or request.get("command") not in REQUEST_COMMANDS
                or not isinstance(request.get("arguments"), dict)):
            return self._reply(request_id if isinstance(request_id, str) else None,
                               False, error_code="REQUEST_INVALID")
        with self._lock:
            if request_id in self._request_ids:
                return self._reply(request_id, False, error_code="REQUEST_REPLAYED")
            is_critical_stop = request["command"] in {"task.stop", "ui.stop"}
            if len(self._request_ids) >= MAX_REQUEST_IDS and not is_critical_stop:
                return self._reply(request_id, False, error_code="REQUEST_LEDGER_FULL")
            now = self._clock()
            self._request_times = [stamp for stamp in self._request_times if now - stamp < 60]
            if len(self._request_times) >= MAX_REQUESTS_PER_MINUTE and not is_critical_stop:
                return self._reply(request_id, False, error_code="RATE_LIMITED")
            if len(self._request_ids) < MAX_REQUEST_IDS:
                self._request_ids.add(request_id)
            if not is_critical_stop:
                self._request_times.append(now)
            dispatcher = self._dispatcher
        if dispatcher is None:
            return self._reply(request_id, False, error_code="MANAGER_NOT_READY")
        try:
            data = dispatcher(request["command"], request["arguments"])
            encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > MAX_REPLY_BYTES:
                return self._reply(request_id, False, error_code="REPLY_TOO_LARGE")
            return self._reply(request_id, True, data=data)
        except ValueError as exc:
            code = str(exc) if re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", str(exc)) else "ARGUMENT_INVALID"
            return self._reply(request_id, False, error_code=code)
        except RuntimeError as exc:
            message = str(exc)
            if message == "Another manager operation is already active":
                code = "MANAGER_BUSY"
            elif message in SAFE_RUNTIME_ERROR_CODES:
                code = message
            else:
                code = "MANAGER_COMMAND_FAILED"
            return self._reply(request_id, False, error_code=code)
        except Exception:
            return self._reply(request_id, False, error_code="MANAGER_COMMAND_FAILED")

    def _runtime_logs(self, limit):
        if type(limit) is not int or not 1 <= limit <= 5000:
            raise ValueError("ARGUMENT_INVALID")
        path = self._log_path
        if path is None:
            root = os.environ.get("LOCALAPPDATA")
            if not root:
                return {"state": "unavailable", "error_code": "LOCALAPPDATA_UNAVAILABLE",
                        "events": [], "event_count": 0}
            path = Path(root) / "AI Virtual Display Manager" / "diagnostics" / "display-events.jsonl"
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return {"state": "ready", "events": [], "event_count": 0,
                    "content_recorded": False}
        if len(raw) > LOG_MAX_BYTES:
            return {"state": "unavailable", "error_code": "LOG_LIMIT_EXCEEDED",
                    "events": [], "event_count": 0}
        rows = []
        rejected = 0
        for line in raw.splitlines():
            if not line:
                continue
            try:
                row = json.loads(line.decode("utf-8", errors="strict"))
            except (UnicodeError, json.JSONDecodeError):
                rejected += 1
                continue
            safe = _safe_log_event(row)
            if safe is None:
                rejected += 1
                continue
            rows.append(safe)
        startup = self._startup_log(path.parent.parent / "startup-error.json")
        return {"state": "ready", "events": rows[-limit:], "event_count": len(rows),
                "rejected_records": rejected, "content_recorded": False,
                "retention_days": 30, "max_bytes": LOG_MAX_BYTES,
                "startup_error": startup}

    @staticmethod
    def _startup_log(path):
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return {"state": "ready", "present": False}
        except OSError:
            return {"state": "unavailable", "error_code": "STARTUP_LOG_READ_FAILED"}
        if len(raw) > STARTUP_LOG_MAX_BYTES:
            return {"state": "unavailable", "error_code": "STARTUP_LOG_LIMIT_EXCEEDED"}
        try:
            record = json.loads(raw.decode("utf-8", errors="strict"))
        except (UnicodeError, json.JSONDecodeError):
            return {"state": "unavailable", "error_code": "STARTUP_LOG_INVALID"}
        if not isinstance(record, dict):
            return {"state": "unavailable", "error_code": "STARTUP_LOG_INVALID"}
        error = record.get("error")
        at = record.get("at")
        if not isinstance(error, str) or len(error) > 4096:
            return {"state": "unavailable", "error_code": "STARTUP_LOG_INVALID"}
        # Older VDM builds persisted "Type: message". Accept that format for
        # compatibility, but return only the validated exception class name.
        error_type = error.split(":", 1)[0] if ":" in error else error
        if (not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", error_type)
                or type(at) not in (int, float) or not 0 <= at <= 9_999_999_999):
            return {"state": "unavailable", "error_code": "STARTUP_LOG_INVALID"}
        return {"state": "ready", "present": True,
                "error_type": error_type, "timestamp": at}

    @staticmethod
    def _validated_topology(topology):
        if not isinstance(topology, dict) or topology.get("topology_verified") is not True:
            raise ValueError("TOPOLOGY_UNVERIFIED")
        displays = topology.get("displays")
        if not isinstance(displays, list) or not displays:
            raise ValueError("TOPOLOGY_UNAVAILABLE")
        for display in displays:
            if (not isinstance(display, dict) or display.get("attached") is not True
                    or display.get("verified") is not True
                    or display.get("topology_verified") is not True
                    or display.get("cloned") is not False
                    or display.get("mirroring_driver") is not False):
                raise ValueError("TOPOLOGY_UNVERIFIED")
        virtual = [item for item in displays if item.get("virtual") is True]
        if any(item.get("monitor_identity_verified") is not True for item in virtual):
            raise ValueError("ENDPOINT_IDENTITY_UNVERIFIED")
        return displays, virtual

    @staticmethod
    def _output_reconciliation(topology, pool):
        """Report attached MTT outputs that are not covered by a live pool session.

        This is diagnostic only. A retired journal or a count match is not proof
        that the manager owns an attached output, so this path never authorizes
        a topology mutation.
        """
        base = {"mtt_output_count": None, "pool_state": None,
                "lease_count": None, "cleanup_eligible": False,
                "physical_primary_verified": False}
        if (not isinstance(topology, dict) or topology.get("topology_verified") is not True
                or not isinstance(topology.get("displays"), list)):
            return {**base, "state": "unverified", "reason": "topology_unverified"}
        if not isinstance(pool, dict):
            return {**base, "state": "unverified", "reason": "pool_state_unavailable"}

        physical_primary = any(
            isinstance(row, dict) and row.get("virtual") is False
            and row.get("primary") is True and row.get("attached") is True
            and row.get("verified") is True and row.get("topology_verified") is True
            and row.get("monitor_identity_verified") is True
            and row.get("cloned") is False and row.get("mirroring_driver") is False
            for row in topology["displays"])
        base["physical_primary_verified"] = physical_primary

        mtt_id = topology.get("mtt_device_instance_id")
        if not isinstance(mtt_id, str) or not mtt_id.strip():
            return {**base, "state": "unverified", "reason": "mtt_adapter_identity_unavailable"}
        attached_mtt = []
        for row in topology["displays"]:
            if not isinstance(row, dict):
                return {**base, "state": "unverified", "reason": "display_record_invalid"}
            parent = row.get("monitor_parent")
            if isinstance(parent, str) and parent.casefold() == mtt_id.casefold():
                if (row.get("virtual") is not True or row.get("attached") is not True
                        or row.get("verified") is not True
                        or row.get("topology_verified") is not True
                        or row.get("monitor_identity_verified") is not True
                        or row.get("cloned") is not False
                        or row.get("mirroring_driver") is not False):
                    return {**base, "state": "unverified", "reason": "mtt_endpoint_identity_unverified"}
                attached_mtt.append(row)
            elif row.get("virtual") is True and (not isinstance(parent, str) or not parent.strip()):
                return {**base, "state": "unverified", "reason": "virtual_parent_unavailable"}

        pool_state = pool.get("state")
        leases = pool.get("leases")
        if (not isinstance(pool_state, str) or not pool_state
                or type(leases) is not int or leases < 0):
            return {**base, "state": "unverified", "reason": "pool_identity_invalid"}
        base.update(mtt_output_count=len(attached_mtt), pool_state=pool_state,
                    lease_count=leases)
        if pool_state == "off" and leases == 0:
            if attached_mtt:
                return {**base, "state": "unmanaged_mtt_output",
                        "reason": "no_active_pool_ownership; origin_unverified"}
            return {**base, "state": "clean", "reason": "no_attached_mtt_output"}
        if pool_state in {"active", "active_unverified"}:
            if leases:
                return {**base, "state": "active_lease",
                        "reason": "pool_has_live_lease; endpoint_ownership_not_exposed"}
            return {**base, "state": "idle_pool_retained",
                    "reason": "pool_session_waiting_for_idle_cleanup"}
        if pool_state in {"cleanup_pending", "recovery_required"}:
            return {**base, "state": "cleanup_pending",
                    "reason": "pool_recovery_requires_exact_ownership"}
        return {**base, "state": "unverified", "reason": "pool_state_unrecognized"}

    @staticmethod
    def _topology_digest(topology, pool):
        # These values describe when/how topology was observed, not topology
        # identity. They change on every refresh and must not invalidate a plan.
        stable_topology = dict(topology) if isinstance(topology, dict) else topology
        if isinstance(stable_topology, dict):
            for key in ("captured_at_unix_ms", "monitor_identity_age_ms",
                        "monitor_identity_cache_hit"):
                stable_topology.pop(key, None)
        stable_pool = {key: pool.get(key) for key in (
            "state", "output_count", "previous_count", "profile", "error", "leases", "last_activity")}
        body = json.dumps({"topology": stable_topology, "pool": stable_pool}, sort_keys=True,
                          ensure_ascii=True, separators=(",", ":"), default=str)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def _dispatch_command(self, command, args):
        if command.startswith("ui."):
            if args or command not in {"ui.show", "ui.hide", "ui.stop", "ui.status"}:
                raise ValueError("ARGUMENT_INVALID")
            callback = getattr(self, "_ui_action", None)
            if callback is None:
                raise ValueError("UI_CONTROL_UNAVAILABLE")
            return callback(command.removeprefix("ui."))
        if command == "status":
            if args:
                raise ValueError("ARGUMENT_INVALID")
            return self._status_reader()
        if command == "logs":
            if set(args) - {"limit"}:
                raise ValueError("ARGUMENT_INVALID")
            return self._runtime_logs(args.get("limit", 100))
        if command == "screens":
            if args:
                raise ValueError("ARGUMENT_INVALID")
            try:
                topology = self._topology_reader()
            except Exception as exc:
                return {"state": "unavailable", "error_code": type(exc).__name__,
                        "topology_verified": False, "displays": []}
            if not isinstance(topology, dict):
                return {"state": "unavailable", "error_code": "TOPOLOGY_RESPONSE_INVALID",
                        "topology_verified": False, "displays": []}
            displays = topology.get("displays")
            if not isinstance(displays, list):
                displays = []
            safe_displays = []
            for display in displays[:32]:
                if not isinstance(display, dict):
                    continue
                safe_displays.append({key: display.get(key) for key in SCREEN_FIELDS
                                      if key in display})
            try:
                pool = self._pool_status()
                pool_state = pool if isinstance(pool, dict) else {
                    "state": "unavailable", "error_code": "POOL_RESPONSE_INVALID"}
            except Exception as exc:
                pool_state = {"state": "unavailable", "error_code": type(exc).__name__}
            verified = topology.get("topology_verified") is True and len(safe_displays) == len(displays)
            return {"state": "verified" if verified else "unverified",
                    "topology_verified": topology.get("topology_verified") is True,
                    "display_count": len(safe_displays), "displays": safe_displays,
                    "pool": pool_state,
                    "output_reconciliation": self._output_reconciliation(topology, pool_state),
                    "captured_at_unix_ms": int(self._clock() * 1000),
                    "pixels_captured": False, "window_content_read": False}
        if command == "stardesk.status":
            if args:
                raise ValueError("ARGUMENT_INVALID")
            reader = getattr(self, "_stardesk_reader", None)
            if reader is None:
                return {"state": "unavailable", "error_code": "STAR_DESK_OBSERVER_UNAVAILABLE",
                        "remote_host_session": "unknown", "remote_input_mode": "unknown",
                        "read_only": True}
            result = reader()
            if not isinstance(result, dict):
                return {"state": "unavailable", "error_code": "STAR_DESK_RESPONSE_INVALID",
                        "remote_host_session": "unknown", "remote_input_mode": "unknown",
                        "read_only": True}
            return result
        if command == "windows":
            if args:
                raise ValueError("ARGUMENT_INVALID")
            try:
                result = self._window_reader()
            except Exception as exc:
                error_code = ("MANAGER_BUSY" if isinstance(exc, RuntimeError)
                              and str(exc) == "Another manager operation is already active"
                              else type(exc).__name__)
                return {"state": "unavailable", "error_code": error_code,
                        "windows": [], "virtual_outputs": []}
            if not isinstance(result, dict) or not isinstance(result.get("windows"), list):
                return {"state": "unavailable", "error_code": "WINDOW_RESPONSE_INVALID",
                        "windows": [], "virtual_outputs": []}
            safe_windows = []
            for row in result["windows"][:500]:
                if not isinstance(row, dict):
                    continue
                pid, bounds = row.get("pid"), row.get("bounds")
                if type(pid) is not int or pid <= 0 or not (
                        isinstance(bounds, list) and len(bounds) == 4
                        and all(type(value) is int and abs(value) <= 1_000_000 for value in bounds)):
                    continue
                app_value = row.get("app")
                app = app_value if isinstance(app_value, str) and app_value in APP_KINDS else "other"
                state = row.get("window_state")
                state = state if isinstance(state, str) and state in WINDOW_STATES else "unknown"
                display = row.get("display_name")
                safe_windows.append({"app": app, "pid": pid, "bounds": bounds,
                                     "window_state": state,
                                     "display_name": display[:64] if isinstance(display, str) else None})
            safe_outputs = []
            outputs = result.get("virtual_outputs", [])
            if isinstance(outputs, list):
                for row in outputs[:32]:
                    if not isinstance(row, dict):
                        continue
                    bounds = row.get("bounds")
                    if not (isinstance(bounds, list) and len(bounds) == 4
                            and all(type(value) is int and abs(value) <= 1_000_000 for value in bounds)):
                        continue
                    name = row.get("display_name")
                    safe_outputs.append({"display_name": name[:64] if isinstance(name, str) else None,
                                         "bounds": bounds, "width": row.get("width"),
                                         "height": row.get("height")})
            return {"state": "verified", "windows": safe_windows,
                    "virtual_outputs": safe_outputs,
                    "movement_requires_confirmation": True,
                    "titles_read": False, "pixels_captured": False}
        if command == "outputs.preview":
            return self._create_output_plan(args)
        if command == "outputs.apply":
            return self._apply_output_plan(args)
        if command == "outputs.return_preview":
            return self._create_return_plan(args)
        if command == "outputs.return":
            return self._return_outputs(args)
        if command.startswith("task."):
            action = command.removeprefix("task.")
            expected = {"status": set(),
                        "start": {"confirmed", "output_count", "profile"},
                        "observe": {"session_id"}, "stop": {"session_id"}}
            fields = set(args) - {"owner_id"} - ({"target_app_id"} if action == "start" else set())
            if action not in expected or fields != expected[action]:
                raise ValueError("ARGUMENT_INVALID")
            target_app_id = args.get("target_app_id", "only_chat_qa")
            if action == "start" and target_app_id not in {
                    "only_chat_qa", "pomodoro", "ai_account_router_qa"}:
                raise ValueError("ARGUMENT_INVALID")
            if action == "start" and args.get("confirmed") is not True:
                raise ValueError("CONFIRMATION_REQUIRED")
            if action == "start" and (type(args.get("output_count")) is not int
                    or args["output_count"] != 0
                    or args.get("profile") != "private"):
                raise ValueError("OUTPUT_REQUEST_INVALID")
            session_id = args.get("session_id")
            if action in {"observe", "stop"} and (not isinstance(session_id, str)
                    or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", session_id, re.I)):
                raise ValueError("SESSION_ID_INVALID")
            request = {"protocol_version": 2, "request_id": str(uuid.uuid4()),
                       "action": action}
            if "owner_id" in args:
                owner = args["owner_id"]
                if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", owner):
                    raise ValueError("OWNER_ID_INVALID")
                request["owner_id"] = owner
            if action == "start":
                request.update(target_app_id=target_app_id, confirmed=True,
                               output_request={"output_count": args["output_count"],
                                               "profile": args["profile"]})
            elif action in {"observe", "stop"}:
                request["session_id"] = session_id
            result = self._task_action(action, request)
            if not isinstance(result, dict):
                raise ValueError("TASK_RUNTIME_RESPONSE_INVALID")
            packet = result.get("packet")
            if packet is not None:
                if not isinstance(packet, bytes) or not 0 < len(packet) <= MAX_PACKET_BYTES:
                    raise ValueError("OBSERVATION_PACKET_INVALID")
                result = dict(result)
                result.pop("packet")
                result["packet_base64"] = base64.b64encode(packet).decode("ascii")
            encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > MAX_REPLY_BYTES:
                raise ValueError("REPLY_TOO_LARGE")
            return result
        raise ValueError("COMMAND_UNSUPPORTED")

    def _read_output_state(self):
        topology = self._topology_reader()
        displays, virtual = self._validated_topology(topology)
        pool = self._pool_status()
        summaries = [{key: display.get(key) for key in (
            "display_name", "target_name", "width", "height", "refresh_rate",
            "virtual", "primary", "cloned", "monitor_identity_verified", "monitor_path")}
            for display in displays]
        return topology, pool, virtual, summaries

    def _create_output_plan(self, args):
        if set(args) != {"count", "profile"}:
            raise ValueError("ARGUMENT_INVALID")
        count, profile = args["count"], args["profile"]
        if (type(count) is not int or not 2 <= count <= 8
                or not isinstance(profile, str) or profile not in PROFILES):
            raise ValueError("ARGUMENT_INVALID")
        topology, pool, virtual, displays = self._read_output_state()
        if pool.get("state") != "off":
            raise ValueError("OUTPUT_POOL_NOT_OFF")
        mode_evidence = "active_mtt_outputs"
        configured_sha256 = None
        common = None
        for display in virtual:
            modes = {tuple(mode) for mode in display.get("available_modes", [])
                     if isinstance(mode, (list, tuple)) and len(mode) == 3
                     and all(type(value) is int for value in mode)}
            common = modes if common is None else common & modes
        if virtual:
            supported = sorted(common or set())
        else:
            try:
                evidence = self._configured_mode_reader()
            except Exception:
                raise ValueError("OUTPUT_MODE_EVIDENCE_UNAVAILABLE") from None
            if (not isinstance(evidence, dict) or set(evidence) != {"modes", "sha256"}
                    or not isinstance(evidence.get("sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", evidence["sha256"])):
                raise ValueError("OUTPUT_MODE_EVIDENCE_UNAVAILABLE")
            configured_modes = evidence.get("modes")
            if not isinstance(configured_modes, (list, tuple)) or len(configured_modes) > 2048:
                raise ValueError("OUTPUT_MODE_EVIDENCE_UNAVAILABLE")
            supported_set = set()
            for mode in configured_modes:
                if (not isinstance(mode, (list, tuple)) or len(mode) != 3
                        or any(type(value) is not int for value in mode)
                        or not 1 <= mode[0] <= 16_384 or not 1 <= mode[1] <= 16_384
                        or not 1 <= mode[2] <= 1000):
                    raise ValueError("OUTPUT_MODE_EVIDENCE_UNAVAILABLE")
                supported_set.add(tuple(mode))
            supported = sorted(supported_set)
            configured_sha256 = evidence["sha256"]
            mode_evidence = "configured_vdd_settings"
        requested_mode = tuple(PROFILES[profile])
        if requested_mode not in supported:
            raise ValueError("PROFILE_UNSUPPORTED")
        total_count = len(virtual) + count
        if total_count > 8:
            raise ValueError("OUTPUT_LIMIT_EXCEEDED")
        plan_id = self._id_factory()
        if not isinstance(plan_id, str) or not PLAN_ID_RE.fullmatch(plan_id):
            raise RuntimeError("Invalid manager plan token")
        expires_at = self._clock() + PLAN_TTL_SECONDS
        fingerprint = self._topology_digest(topology, pool)
        self._plans[plan_id] = {"kind": "create", "count": count, "profile": profile,
                                "total_count": total_count,
                                "fingerprint": fingerprint, "expires_at": expires_at,
                                "supported_modes": [list(mode) for mode in supported],
                                "mode_evidence": mode_evidence,
                                "configured_modes_sha256": configured_sha256}
        return {"state": "preview", "plan_id": plan_id,
                "expires_in_seconds": PLAN_TTL_SECONDS,
                "confirmation": f"CREATE {count} {profile.upper()} {plan_id[:8]}",
                "existing_mtt_outputs": len(virtual), "new_outputs": count,
                "total_mtt_outputs": total_count,
                "profile": profile, "mode": list(requested_mode),
                "supported_mode_count": len(supported), "mode_evidence": mode_evidence,
                "displays": displays,
                "pool_state": pool.get("state")}

    def _take_plan(self, args, kind):
        if set(args) != {"plan_id", "confirm"}:
            raise ValueError("ARGUMENT_INVALID")
        plan_id, confirm = args["plan_id"], args["confirm"]
        if not isinstance(plan_id, str) or not PLAN_ID_RE.fullmatch(plan_id):
            raise ValueError("PLAN_INVALID")
        plan = self._plans.pop(plan_id, None)
        if plan is None or plan.get("kind") != kind:
            raise ValueError("PLAN_EXPIRED_OR_UNKNOWN")
        if self._clock() > plan["expires_at"]:
            raise ValueError("PLAN_EXPIRED_OR_UNKNOWN")
        expected = (f"CREATE {plan['count']} {plan['profile'].upper()} {plan_id[:8]}"
                    if kind == "create" else f"RETURN POOL {plan_id[:8]}")
        if not isinstance(confirm, str) or not secrets.compare_digest(confirm, expected):
            raise ValueError("CONFIRMATION_MISMATCH")
        return plan

    def _apply_output_plan(self, args):
        plan = self._take_plan(args, "create")
        topology, pool, _virtual, _displays = self._read_output_state()
        if pool.get("state") != "off" or self._topology_digest(topology, pool) != plan["fingerprint"]:
            raise ValueError("LIVE_STATE_CHANGED")
        if plan.get("mode_evidence") == "configured_vdd_settings":
            try:
                evidence = self._configured_mode_reader()
            except Exception:
                raise ValueError("OUTPUT_MODE_EVIDENCE_UNAVAILABLE") from None
            if (not isinstance(evidence, dict)
                    or evidence.get("sha256") != plan.get("configured_modes_sha256")):
                raise ValueError("LIVE_STATE_CHANGED")
        return self._pool_begin({"output_count": plan["total_count"], "profile": plan["profile"],
                                 "supported_modes": plan["supported_modes"], "confirmed": True})

    def _create_return_plan(self, args):
        if args:
            raise ValueError("ARGUMENT_INVALID")
        topology, pool, _virtual, displays = self._read_output_state()
        if pool.get("state") == "off":
            raise ValueError("OUTPUT_POOL_ALREADY_OFF")
        plan_id = self._id_factory()
        if not isinstance(plan_id, str) or not PLAN_ID_RE.fullmatch(plan_id):
            raise RuntimeError("Invalid manager plan token")
        self._plans[plan_id] = {"kind": "return",
                                "fingerprint": self._topology_digest(topology, pool),
                                "expires_at": self._clock() + PLAN_TTL_SECONDS}
        return {"state": "preview", "plan_id": plan_id,
                "expires_in_seconds": PLAN_TTL_SECONDS,
                "confirmation": f"RETURN POOL {plan_id[:8]}",
                "pool": pool, "displays": displays,
                "notice": "VDM will revalidate exact endpoints and saved modes; uncertainty keeps outputs active."}

    def _return_outputs(self, args):
        plan = self._take_plan(args, "return")
        topology, pool, _virtual, _displays = self._read_output_state()
        if pool.get("state") == "off" or self._topology_digest(topology, pool) != plan["fingerprint"]:
            raise ValueError("LIVE_STATE_CHANGED")
        return self._pool_stop()

    def refresh(self):
        server = self._server
        servers = (server, self._control_server, self._ui_control_server)
        return {"module_id": MODULE_ID, "version": VERSION, "protocol_version": PROTOCOL_VERSION,
                "pipe_ready": bool(server and server.ready),
                "control_pipe_ready": bool(self._control_server and self._control_server.ready),
                "ui_control_pipe_ready": bool(self._ui_control_server and self._ui_control_server.ready),
                "active_clients": sum(int(s.active_clients) for s in servers if s),
                "startup_error": self._startup_error,
                "commands": sorted(REQUEST_COMMANDS)}

    def action(self, name, _payload):
        if name != "status":
            raise ValueError("COMMAND_UNSUPPORTED")
        return self.refresh()

    def snapshot(self):
        servers = [s for s in (self._server, self._control_server, self._ui_control_server) if s]
        if any(s.active_clients for s in servers):
            raise RuntimeError("Manager CLI request is active; reload is deferred")
        if any(s.ready for s in servers):
            raise RuntimeError("The manager CLI pipe requires a manager restart to reload")
        return {"inactive": True}

    def restore(self, state):
        if state not in (None, {"inactive": True}):
            raise ValueError("Manager CLI runtime state cannot be restored")

    def reload_blocked(self):
        return any(s.active_clients for s in
                   (self._server, self._control_server, self._ui_control_server) if s)

    def dispose(self):
        with self._lock:
            servers = [s for s in (self._server, self._control_server, self._ui_control_server) if s]
            if any(s.active_clients for s in servers):
                raise RuntimeError("Manager CLI requests are active")
            self._accepting = False
        for server in servers:
            server.stop()
        with self._lock:
            self._server = None
            self._control_server = None
            self._ui_control_server = None
            self._dispatcher = None
            self._plans.clear()
            self._accepting = False
            self._initialized = False


def create(_context=None):
    return ManagerCliModule()


