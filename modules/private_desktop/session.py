"""Private-desktop diagnostic session owner.

This deliberately supports only the manager's own test application. It is a
feasibility slice for a future consumer adapter, not a generic app launcher.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import ntpath
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path

from core.private_capture_codes import PRIVATE_DESKTOP_CAPTURE_DIAGNOSTIC_CODES
from core.png_capture import MAX_PNG_BYTES, PNG_SIGNATURE
CREATE_SUSPENDED = 0x00000004
DESKTOP_READOBJECTS = 0x0001
DESKTOP_ACCESS = 0x00C3
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
WAIT_OBJECT_0 = 0
_STARTUP_CLEANUP_FIELDS = (
    "stopped", "target_stopped", "desktop_thread_restored",
    "desktop_handle_closed", "desktop_removed", "owned_window_removed",
    "job_handle_closed", "input_desktop_unchanged",
)
_STARTUP_RESOURCE_FIELDS = (
    "desktop_created", "job_created", "process_created", "window_created",
)
STARTUP_ERROR_CODES = frozenset({
    "PRIVATE_DESKTOP_START_FAILED",
    "HOST_INPUT_DESKTOP_ACCESS_DENIED",
    "HOST_INPUT_DESKTOP_UNKNOWN",
    "HOST_INPUT_DESKTOP_CHANGED",
})


def _startup_error_code(phase: str, exc: BaseException) -> str:
    """Expose only a stable, non-sensitive reason for a failed startup gate."""
    message = str(exc)
    if phase in {"input_desktop_preflight", "input_desktop_postflight"}:
        if message == "HOST_INPUT_DESKTOP_UNKNOWN":
            return "HOST_INPUT_DESKTOP_UNKNOWN"
        if message in {"HOST_INPUT_DESKTOP_INTERRUPTED",
                       "HOST_INPUT_DESKTOP_CHANGED_DURING_START"}:
            return "HOST_INPUT_DESKTOP_CHANGED"
        if (isinstance(exc, OSError)
                and (getattr(exc, "winerror", None) == 5 or getattr(exc, "errno", None) == 5)):
            return "HOST_INPUT_DESKTOP_ACCESS_DENIED"
    return "PRIVATE_DESKTOP_START_FAILED"


def _startup_cleanup_verified(receipt) -> bool:
    return (isinstance(receipt, dict)
            and all(receipt.get(field) is True for field in _STARTUP_CLEANUP_FIELDS))


def startup_resources_absent_verified(receipt) -> bool:
    """Prove preflight stopped before creating any owned desktop/session resource."""
    return (isinstance(receipt, dict)
            and all(receipt.get(field) is False for field in _STARTUP_RESOURCE_FIELDS))


class DiagnosticSessionStartFailure(RuntimeError):
    """Startup failed; retain the session owner while cleanup may still be pending."""

    def __init__(self, message: str, session=None, cleanup_receipt=None,
                 error_code: str | None = None):
        super().__init__(message)
        self.session = session
        self.error_code = (error_code if error_code in STARTUP_ERROR_CODES
                           else "PRIVATE_DESKTOP_START_FAILED")
        self.cleanup_receipt = (dict(cleanup_receipt)
                                if isinstance(cleanup_receipt, dict) else None)
        self.cleanup_verified = _startup_cleanup_verified(self.cleanup_receipt)


class DiagnosticSessionStartError(DiagnosticSessionStartFailure):
    """Startup failed after the owner proved the target absent and cleanup complete."""

    def __init__(self, message: str, cleanup_receipt: dict, session=None,
                 error_code: str | None = None):
        if not _startup_cleanup_verified(cleanup_receipt):
            raise ValueError("A complete private-desktop cleanup receipt is required")
        super().__init__(message, session, cleanup_receipt, error_code)


def _read_input_desktop_name(api, manager_thread_id: int | None = None):
    return (api.input_desktop_name() if manager_thread_id is None else
            api.input_desktop_name(manager_thread_id))


def _is_window_on_input_desktop(api, hwnd: int,
                                manager_thread_id: int | None = None):
    return (api.window_is_on_input_desktop(hwnd) if manager_thread_id is None else
            api.window_is_on_input_desktop(hwnd, manager_thread_id))


def _input_desktop_interruption(api, expected_name: str,
                                manager_thread_id: int | None = None) -> dict | None:
    """Return a fail-closed interruption result if the host input desktop changed."""
    try:
        current = _read_input_desktop_name(api, manager_thread_id)
    except Exception:
        current = "unknown"
    if current == expected_name:
        return None
    code = ("HOST_INPUT_DESKTOP_UNKNOWN" if current == "unknown"
            else "HOST_INPUT_DESKTOP_CHANGED")
    return {"error_code": code, "input_desktop_before": expected_name,
            "input_desktop_current": current}


def _window_absent_from_input_desktop(api, hwnd: int,
                                      manager_thread_id: int | None = None) -> bool:
    """Prove the exact owned HWND is not enumerated on the host input desktop."""
    try:
        return not _is_window_on_input_desktop(api, hwnd, manager_thread_id)
    except Exception:
        return False


def _window_exists(api, desktop, hwnd: int, expected_pid: int | None = None,
                   expected_title: str | None = None,
                   expected_class: str | None = None) -> bool:
    if not desktop or not hwnd:
        return False
    found = []
    @api.CALLBACK
    def collect(candidate, _param):
        if int(candidate) == int(hwnd):
            title = ctypes.create_unicode_buffer(260)
            api.user32.GetWindowTextW(candidate, title, len(title))
            class_name = ctypes.create_unicode_buffer(260)
            api.user32.GetClassNameW(candidate, class_name, len(class_name))
            pid = wintypes.DWORD()
            api.user32.GetWindowThreadProcessId(candidate, ctypes.byref(pid))
            if ((expected_pid is None or int(pid.value) == expected_pid)
                    and (expected_title is None or title.value == expected_title)
                    and (expected_class is None
                         or _matches_window_class_pattern(class_name.value, expected_class))):
                found.append(True)
        return True
    try:
        ctypes.set_last_error(0)
        ok = api.user32.EnumDesktopWindows(desktop, collect, 0)
        if not ok and ctypes.get_last_error():
            return True
        return bool(found)
    except Exception:
        return True


def _matches_window_class_pattern(actual: str, pattern: str) -> bool:
    """Match exact Win32 classes or a strictly bounded trailing-star prefix."""
    if not isinstance(actual, str) or not isinstance(pattern, str):
        return False
    if "*" not in pattern:
        return actual == pattern
    if not pattern.endswith("*") or pattern.count("*") != 1:
        return False
    prefix = pattern[:-1]
    if (len(prefix) < 12 or not prefix.isascii()
            or any(not (char.isalnum() or char in "._-") for char in prefix)):
        return False
    return len(actual) > len(prefix) and actual.startswith(prefix)


class DiagnosticSession:
    """One bounded, caller-driven lifecycle for the self-authored test window."""

    SESSION_IDLE_SECONDS = 300
    ACTION_TIMEOUT_SECONDS = 12

    def __init__(self, api, startup_info_type, process_info_type,
                 extended_limit_info_type, *, target_descriptor=None,
                 session_id=None):
        self.api = api
        self._startup_info_type = startup_info_type
        self._process_info_type = process_info_type
        self._extended_limit_info_type = extended_limit_info_type
        self.session_id = session_id or str(uuid.uuid4())
        self.target_descriptor = target_descriptor
        self.desktop_name = f"AIVDDS_{uuid.uuid4().hex[:12]}"
        # Pin the caller's host desktop thread. The session owner may run on
        # a different desktop after CreateDesktopW; fallback checks must not
        # infer the host desktop from that worker's current assignment.
        self._host_thread_id = None
        try:
            candidate_thread_id = int(api.kernel32.GetCurrentThreadId())
            if candidate_thread_id > 0:
                self._host_thread_id = candidate_thread_id
        except (AttributeError, TypeError, ValueError, OSError):
            pass
        self._commands: queue.Queue = queue.Queue(maxsize=1)
        self._pending_action_descriptor = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._owner, name="private-desktop-session",
                                        daemon=True)
        self._closed = False
        self._stop_requested = False
        self._state: dict = {"state": "starting"}
        self._lock = threading.Lock()
        try:
            self._thread.start()
        except Exception as exc:
            raise DiagnosticSessionStartFailure(
                f"Private-desktop diagnostic worker could not start: {type(exc).__name__}",
                self,
            ) from exc
        if not self._ready.wait(self.ACTION_TIMEOUT_SECONDS):
            try:
                receipt = self.close()
            except Exception as exc:
                receipt = self._state.get("stop_receipt")
                if (not self._thread.is_alive()
                        and _startup_cleanup_verified(receipt)):
                    self._closed = True
                    raise DiagnosticSessionStartError(
                        "Private-desktop diagnostic session start timed out", receipt,
                        session=self, error_code=self._state.get("error_code"),
                    ) from exc
                raise DiagnosticSessionStartFailure(
                    "Private-desktop diagnostic session start timed out", self, receipt,
                ) from exc
            if (not self._thread.is_alive()
                    and _startup_cleanup_verified(receipt)):
                self._closed = True
                raise DiagnosticSessionStartError(
                    "Private-desktop diagnostic session start timed out", receipt,
                    session=self, error_code=self._state.get("error_code"),
                )
            raise DiagnosticSessionStartFailure(
                "Private-desktop diagnostic session did not start", self, receipt)
        if self._state.get("state") != "active":
            error = self._state.get("error", "PRIVATE_DESKTOP_SESSION_START_FAILED")
            self._thread.join(timeout=self.ACTION_TIMEOUT_SECONDS)
            if self._thread.is_alive():
                raise DiagnosticSessionStartFailure(
                    f"{error}; PRIVATE_DESKTOP_CLEANUP_PENDING", self,
                    self._state.get("stop_receipt"),
                    error_code=self._state.get("error_code"),
                )
            receipt = self._state.get("stop_receipt", {})
            if not _startup_cleanup_verified(receipt):
                raise DiagnosticSessionStartFailure(
                    f"{error}; PRIVATE_DESKTOP_CLEANUP_FAILED", self, receipt,
                    error_code=self._state.get("error_code"))
            self._closed = True
            raise DiagnosticSessionStartError(
                error, receipt, session=self, error_code=self._state.get("error_code"))

    @property
    def active(self):
        return not self._closed and self._state.get("state") == "active"

    def identity(self) -> dict:
        """Return the immutable, manager-owned target identity proof for this session."""
        state = dict(self._state)
        if state.get("state") != "active" or self._closed:
            raise RuntimeError("Private-desktop diagnostic session is not active")
        return {
            "process_id": state.get("target_pid"),
            "window_id": state.get("window_id"),
            "job_owned": state.get("job_owned") is True,
            "window_absent_from_input_desktop":
                state.get("window_absent_from_input_desktop") is True,
            "input_desktop_unchanged": state.get("input_desktop_unchanged") is True,
            "input_desktop_before": state.get("input_desktop_before"),
            "input_desktop_after": state.get("input_desktop_after"),
        }

    def request(self, action: str, descriptor=None) -> dict:
        if action not in {"observe", "invoke", "stop"}:
            raise ValueError("Unsupported diagnostic-session action")
        wait_for_existing_stop = False
        with self._lock:
            if self._closed and action != "stop":
                raise RuntimeError("Diagnostic session is already closed")
            if action == "stop" and self._closed:
                receipt = self._state.get("stop_receipt")
                if not isinstance(receipt, dict):
                    raise RuntimeError("Private-desktop stop receipt is unavailable")
                return dict(receipt)
            if action == "stop" and not self._thread.is_alive():
                self._closed = True
                receipt = self._state.get("stop_receipt")
                if not isinstance(receipt, dict):
                    raise RuntimeError("Private-desktop stop receipt is unavailable")
                return dict(receipt)
            if action == "stop" and self._stop_requested:
                wait_for_existing_stop = True
            else:
                if self._stop_requested:
                    raise RuntimeError("Private-desktop session is stopping")
                if self._commands.full():
                    raise RuntimeError("A diagnostic-session action is already pending")
                response: queue.Queue = queue.Queue(maxsize=1)
                self._pending_action_descriptor = descriptor
                self._commands.put_nowait((action, response))
                if action == "stop":
                    self._stop_requested = True
        if wait_for_existing_stop:
            return self._wait_for_stop()
        try:
            result = response.get(timeout=self.ACTION_TIMEOUT_SECONDS)
        except queue.Empty:
            # A timed-out observation/action must not leave a private app running
            # unattended. Queue stop behind an in-flight request when possible;
            # if another request is already queued, the owner checks
            # _stop_requested before executing it and exits after that request.
            with self._lock:
                self._stop_requested = True
                if self._commands.empty():
                    stop_response: queue.Queue = queue.Queue(maxsize=1)
                    try:
                        self._commands.put_nowait(("stop", stop_response))
                    except queue.Full:
                        pass
            raise TimeoutError("Private-desktop session action exceeded its bound")
        if action == "stop":
            return self._wait_for_stop()
        return result

    def _wait_for_stop(self) -> dict:
        self._thread.join(timeout=3)
        if self._thread.is_alive():
            raise TimeoutError("Private-desktop session cleanup exceeded its bound")
        with self._lock:
            self._closed = True
            receipt = self._state.get("stop_receipt")
        if not isinstance(receipt, dict):
            raise RuntimeError("Private-desktop stop receipt is unavailable")
        return dict(receipt)

    def close(self):
        if self._closed or not self._thread.is_alive():
            with self._lock:
                receipt = self._state.get("stop_receipt")
            if not isinstance(receipt, dict):
                raise RuntimeError("Private-desktop stop receipt is unavailable")
            self._closed = True
            return dict(receipt)
        return self.request("stop")

    def _owner(self):
        api = self.api
        u, k = api.user32, api.kernel32
        original = u.GetThreadDesktop(k.GetCurrentThreadId())
        desktop = job = None
        process = self._process_info_type()
        process_assigned = False
        thread_restored = False
        desktop_closed = False
        job_closed = False
        target_stopped = False
        target_exit_code = None
        job_terminated = False
        hwnd = None
        window_pid = None
        token = uuid.uuid4().hex[:12]
        descriptor = self.target_descriptor
        title = f"AIQA_PRIVATE_DESKTOP_{token}" if descriptor is None else None
        input_before = None
        phase = "input_desktop_preflight"
        try:
            input_before = _read_input_desktop_name(api, self._host_thread_id)
            self._state["input_desktop_before"] = input_before
            if input_before != "Default":
                code = ("HOST_INPUT_DESKTOP_INTERRUPTED" if input_before != "unknown"
                        else "HOST_INPUT_DESKTOP_UNKNOWN")
                raise RuntimeError(code)
            phase = "create_private_desktop"
            desktop = u.CreateDesktopW(self.desktop_name, None, None, 0,
                                       DESKTOP_ACCESS, None)
            if not desktop:
                raise ctypes.WinError(ctypes.get_last_error())

            startup = self._startup_info_type()
            startup.cb = ctypes.sizeof(startup)
            startup.lpDesktop = f"WinSta0\\{self.desktop_name}"
            if descriptor is None:
                executable, args = api.probe_command(token)
                expected_classes = ()
            else:
                executable, args = api.target_command(descriptor)
                expected_classes = descriptor.window_class_names
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline([executable, *args]))
            job = k.CreateJobObjectW(None, None)
            if not job:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = self._extended_limit_info_type()
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                             ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not k.CreateProcessW(executable, command, None, None, False, CREATE_SUSPENDED,
                                    None, (descriptor.working_directory if descriptor is not None
                                           else str(Path(executable).resolve().parent)),
                                    ctypes.byref(startup), ctypes.byref(process)):
                raise ctypes.WinError(ctypes.get_last_error())
            target_pid = int(k.GetProcessId(process.hProcess))
            if target_pid <= 0:
                raise ctypes.WinError(ctypes.get_last_error())
            if not k.AssignProcessToJobObject(job, process.hProcess):
                raise ctypes.WinError(ctypes.get_last_error())
            process_assigned = True
            if descriptor is not None:
                digest_state = hashlib.sha256()
                with Path(executable).open("rb") as image_stream:
                    for image_block in iter(lambda: image_stream.read(1024 * 1024), b""):
                        digest_state.update(image_block)
                digest = digest_state.hexdigest()
                if digest != descriptor.executable_sha256:
                    raise RuntimeError("TARGET_IMAGE_HASH_MISMATCH_AFTER_CREATE")
            if k.ResumeThread(process.hThread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())

            windows = []
            visible_windows = []
            matching_windows = []
            expected_executable = ntpath.normcase(ntpath.realpath(executable))

            @api.CALLBACK
            def collect(candidate, _param):
                buf = ctypes.create_unicode_buffer(260)
                u.GetWindowTextW(candidate, buf, len(buf))
                pid = wintypes.DWORD()
                thread_id = u.GetWindowThreadProcessId(candidate, ctypes.byref(pid))
                if buf.value and len(visible_windows) < 12:
                    visible_windows.append({"pid": int(pid.value), "title": buf.value[:80]})
                class_buf = ctypes.create_unicode_buffer(260)
                u.GetClassNameW(candidate, class_buf, len(class_buf))
                is_expected_window = (buf.value == title if descriptor is None
                                      else any(_matches_window_class_pattern(
                                          class_buf.value, pattern)
                                          for pattern in expected_classes))
                if is_expected_window:
                    process_handle = k.OpenProcess(0x1000, False, pid.value)
                    if process_handle:
                        try:
                            in_job = wintypes.BOOL()
                            belongs_to_job = bool(k.IsProcessInJob(
                                process_handle, job, ctypes.byref(in_job))) and bool(in_job.value)
                            image = ctypes.create_unicode_buffer(1024)
                            image_len = wintypes.DWORD(len(image))
                            image_ok = bool(k.QueryFullProcessImageNameW(
                                process_handle, 0, image, ctypes.byref(image_len)))
                            image_matches = image_ok and ntpath.normcase(
                                ntpath.realpath(image.value)) == expected_executable
                            matching_windows.append({"pid": int(pid.value),
                                "image": image.value if image_ok else None,
                                "in_owned_job": bool(belongs_to_job),
                                "matches_expected_image": bool(image_matches),
                                "process_info_pid": target_pid})
                            # Source-run Python may transfer execution from the
                            # venv launcher to its base interpreter. Job
                            # membership plus this fresh random title still
                            # proves the HWND belongs to this owned launch tree.
                            if (belongs_to_job and
                                    (descriptor is None or image_matches)):
                                windows.append((int(candidate), int(thread_id), int(pid.value),
                                                class_buf.value))
                        finally:
                            k.CloseHandle(process_handle)
                return True

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not windows:
                ctypes.set_last_error(0)
                ok = u.EnumDesktopWindows(desktop, collect, 0)
                if not ok and ctypes.get_last_error():
                    raise ctypes.WinError(ctypes.get_last_error())
                time.sleep(0.05)
            if not windows:
                raise TimeoutError(
                    f"Owned diagnostic window did not appear; pid={target_pid}; "
                    f"desktop={self.desktop_name}; exe={expected_executable}; "
                    f"title={title}; matching={matching_windows!r}; "
                    f"visible={visible_windows!r}")
            hwnd, _thread_id, window_pid, target_class = windows[0]
            phase = "input_desktop_postflight"
            input_after = _read_input_desktop_name(api, self._host_thread_id)
            if input_after != input_before:
                raise RuntimeError("HOST_INPUT_DESKTOP_CHANGED_DURING_START")
            on_input_desktop = _is_window_on_input_desktop(
                api, hwnd, self._host_thread_id)
            if on_input_desktop:
                raise RuntimeError("Private diagnostic window appeared on the input desktop")
            self._state = {"state": "active", "session_id": self.session_id,
                           "desktop": self.desktop_name, "target_pid": target_pid,
                           "window_pid": window_pid, "job_owned": True,
                           "window_id": int(hwnd), "window_absent_from_input_desktop": True,
                           "input_desktop_before": input_before,
                           "input_desktop_after": input_after,
                           "input_desktop_unchanged": input_before == input_after,
                           "executable_path": str(Path(executable).resolve()),
                           "executable_sha256": (descriptor.executable_sha256
                                                 if descriptor is not None else None),
                           "window_class": target_class if descriptor is not None else None,
                           "display_enabled": False}
            self._ready.set()

            last_activity = time.monotonic()
            while True:
                remaining = max(0.05, self.SESSION_IDLE_SECONDS -
                                (time.monotonic() - last_activity))
                try:
                    action, response = self._commands.get(timeout=min(remaining, 0.5))
                    action_descriptor = self._pending_action_descriptor
                    self._pending_action_descriptor = None
                except queue.Empty:
                    interruption = _input_desktop_interruption(
                        api, self._state.get("input_desktop_before", "unknown"),
                        self._host_thread_id)
                    if interruption:
                        self._state["interruption"] = interruption
                        break
                    if time.monotonic() - last_activity >= self.SESSION_IDLE_SECONDS:
                        self._state["idle_timeout"] = True
                        break
                    if not _window_exists(api, desktop, hwnd, window_pid, title,
                                          target_class if descriptor is not None else None):
                        self._state["target_exited"] = True
                        break
                    continue
                try:
                    with self._lock:
                        stop_before_action = self._stop_requested and action != "stop"
                    if stop_before_action:
                        response.put({"error": "PRIVATE_DESKTOP_SESSION_STOPPING"})
                        break
                    if action == "observe":
                        response.put(self._observe(api, hwnd,
                                                   self._state.get("input_desktop_before"),
                                                   self._host_thread_id))
                    elif action == "invoke":
                        if descriptor is None:
                            invoked = self._invoke(api, hwnd, job, self.desktop_name,
                                                   self._state.get("input_desktop_before"),
                                                   self._host_thread_id)
                        else:
                            invoked = self._invoke_descriptor(
                                api, hwnd, job, self.desktop_name,
                                self._state.get("input_desktop_before"), action_descriptor,
                                self._host_thread_id)
                        response.put(invoked)
                    elif action == "stop":
                        response.put({"stopping": True})
                        break
                    else:
                        response.put({"error": "ACTION_INVALID"})
                    last_activity = time.monotonic()
                    interruption = _input_desktop_interruption(
                        api, self._state.get("input_desktop_before", "unknown"),
                        self._host_thread_id)
                    if interruption:
                        self._state["interruption"] = interruption
                        break
                except Exception as exc:
                    diagnostic_code = getattr(exc, "diagnostic_code", None)
                    if diagnostic_code in PRIVATE_DESKTOP_CAPTURE_DIAGNOSTIC_CODES:
                        response.put({"error_code": diagnostic_code})
                    else:
                        response.put({"error": f"{type(exc).__name__}: {exc}"[:180]})
        except Exception as exc:
            if not self._ready.is_set():
                self._state.update({"state": "failed", "error":
                                    f"{type(exc).__name__}: {exc}"[:900],
                                    "error_code": _startup_error_code(phase, exc),
                                    "error_phase": phase})
                self._ready.set()
        finally:
            if process.hProcess:
                if hwnd:
                    u.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE, exact owned HWND.
                wait = k.WaitForSingleObject(process.hProcess, 2500)
                target_stopped = wait == WAIT_OBJECT_0
                if not target_stopped and job and process_assigned:
                    job_terminated = bool(k.TerminateJobObject(job, 0xDEAD))
                    target_stopped = k.WaitForSingleObject(process.hProcess, 1500) == WAIT_OBJECT_0
                elif not target_stopped and not process_assigned:
                    if k.TerminateProcess(process.hProcess, 0xDEAD):
                        target_stopped = (
                            k.WaitForSingleObject(process.hProcess, 1500) == WAIT_OBJECT_0)
                exit_code = wintypes.DWORD()
                if k.GetExitCodeProcess(process.hProcess, ctypes.byref(exit_code)):
                    target_exit_code = int(exit_code.value)
                if process.hThread:
                    k.CloseHandle(process.hThread)
                k.CloseHandle(process.hProcess)
            else:
                # A launch failure with no process handle leaves no target alive.
                target_stopped = True
            if job and process_assigned and not job_terminated:
                job_terminated = bool(k.TerminateJobObject(job, 0xDEAD))
            if original:
                current = u.GetThreadDesktop(k.GetCurrentThreadId())
                try:
                    thread_restored = api.object_name(current) == api.object_name(original)
                except Exception:
                    thread_restored = False
                if not thread_restored and u.SetThreadDesktop(original):
                    try:
                        thread_restored = api.object_name(
                            u.GetThreadDesktop(k.GetCurrentThreadId())) == api.object_name(original)
                    except Exception:
                        thread_restored = False
            deadline = time.monotonic() + 2.0
            owned_window_removed = not hwnd
            while hwnd and time.monotonic() < deadline:
                if not _window_exists(api, desktop, hwnd, window_pid, title,
                                      target_class if descriptor is not None else None):
                    owned_window_removed = True
                    break
                time.sleep(0.05)
            if job:
                job_closed = bool(k.CloseHandle(job))
            if desktop and thread_restored:
                desktop_closed = bool(u.CloseDesktop(desktop))
            removal = {"desktop_handle_closed": desktop_closed, "desktop_removed": False}
            removal_report: dict = {}
            if desktop_closed:
                deadline = time.monotonic() + 2.0
                last_error = None
                while True:
                    try:
                        if not api.desktop_exists(self.desktop_name):
                            removal["desktop_removed"] = True
                            break
                    except Exception as exc:
                        last_error = exc
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.05)
                if last_error is not None:
                    removal_report["cleanup_error"] = (
                        f"desktop list readback: {type(last_error).__name__}: {last_error}"[:180])
            try:
                input_after = _read_input_desktop_name(api, self._host_thread_id)
            except Exception:
                input_after = "unknown"
            receipt = {"stopped": target_stopped and thread_restored and desktop_closed
                       and job_closed and removal.get("desktop_removed") is True
                       and owned_window_removed
                       and input_after == self._state.get("input_desktop_before"),
                       "desktop_created": desktop is not None,
                       "job_created": job is not None,
                       "process_created": bool(process.hProcess),
                       "window_created": bool(hwnd),
                       "target_stopped": target_stopped,
                       "target_exit_code": target_exit_code,
                       "job_terminated": job_terminated,
                       "desktop_thread_restored": thread_restored,
                       "desktop_handle_closed": desktop_closed,
                       "desktop_removed": removal.get("desktop_removed") is True,
                       "owned_window_removed": owned_window_removed,
                       "job_handle_closed": job_closed,
                       "input_desktop_after": input_after,
                       "input_desktop_unchanged": input_after ==
                           self._state.get("input_desktop_before"),
                       "interruption": self._state.get("interruption")}
            if removal_report.get("cleanup_error"):
                receipt["cleanup_error"] = removal_report["cleanup_error"]
            self._state.update({"state": "closed", "stop_receipt": receipt})
            self._closed = True

    @staticmethod
    def _observe(api, hwnd: int, expected_input_desktop: str,
                 manager_thread_id: int | None = None) -> dict:
        u = api.user32
        interruption = _input_desktop_interruption(api, expected_input_desktop,
                                                    manager_thread_id)
        if interruption:
            return {"session_state": "interrupted", **interruption}
        png, width, height, color_count = api.capture_png(hwnd)
        if not png.startswith(PNG_SIGNATURE) or len(png) > MAX_PNG_BYTES:
            raise ValueError("Captured PNG failed bounds/signature validation")
        title = ctypes.create_unicode_buffer(512)
        u.GetWindowTextW(hwnd, title, len(title))
        return {"session_state": "active", "window_id": int(hwnd),
                "title": title.value[:120], "width": int(width), "height": int(height),
                "input_desktop_unchanged": True,
                "unique_sample_colors": int(color_count), "png_sha256": hashlib.sha256(png).hexdigest(),
                "png_bytes": base64.b64encode(png).decode("ascii")}

    def identity(self) -> dict:
        state = dict(self._state)
        if state.get("state") != "active" or self._closed:
            raise RuntimeError("Private-desktop session is not active")
        return {"process_id": state.get("window_pid"),
                "window_id": state.get("window_id"), "job_owned": True,
                "window_absent_from_input_desktop":
                    state.get("window_absent_from_input_desktop") is True,
                "input_desktop_unchanged": state.get("input_desktop_unchanged") is True,
                "input_desktop_before": state.get("input_desktop_before"),
                "input_desktop_after": state.get("input_desktop_after"),
                "executable_path": state.get("executable_path"),
                "executable_sha256": state.get("executable_sha256")}

    def observe(self, *, timeout_seconds: int):
        result = self.request("observe")
        if result.get("session_state") != "active":
            return result
        try:
            png = base64.b64decode(result.pop("png_bytes"), validate=True)
        except Exception as exc:
            raise RuntimeError("Private target capture packet is invalid") from exc
        result["png_bytes"] = png
        result["content_verified"] = result.get("unique_sample_colors", 0) >= 3
        result["sampled_color_count"] = result.get("unique_sample_colors", 0)
        result["accessibility"] = {
            "window_title": result.get("title", "")[:120],
            "window_class": self._state.get("window_class"),
            "width": result.get("width"), "height": result.get("height")}
        return result

    def invoke(self, action, *, timeout_seconds: int):
        if self.target_descriptor is None or action not in self.target_descriptor.actions:
            raise ValueError("Action is not in the immutable target descriptor")
        return self.request("invoke", action)

    def stop(self, *, timeout_seconds: int):
        return self.request("stop")

    def _invoke_descriptor(self, api, hwnd: int, job, desktop_name: str,
                           expected_input_desktop: str, action,
                           manager_thread_id: int | None = None) -> dict:
        if action not in self.target_descriptor.actions:
            return {"performed": False}
        interruption = _input_desktop_interruption(api, expected_input_desktop,
                                                    manager_thread_id)
        if interruption:
            return {"performed": False, **interruption}
        descriptor_fd, descriptor_path = tempfile.mkstemp(
            prefix="AIVDDS_ACTION_", suffix=".json")
        report_fd, report_path = tempfile.mkstemp(
            prefix="AIVDDS_ACTION_RESULT_", suffix=".json")
        os.close(descriptor_fd)
        os.close(report_fd)
        descriptor_file, report_file = Path(descriptor_path), Path(report_path)
        worker = self._process_info_type()
        k = api.kernel32
        try:
            descriptor_file.write_text(json.dumps({
                "control_type": action.control_type,
                "control_name": action.control_name,
                "postcondition_name": action.postcondition_name,
                "postcondition_value": action.postcondition_value,
            }, ensure_ascii=False), encoding="utf-8")
            executable, args = api.uia_action_worker_command(
                hwnd, str(descriptor_file), str(report_file))
            startup = self._startup_info_type()
            startup.cb = ctypes.sizeof(startup)
            startup.lpDesktop = f"WinSta0\\{desktop_name}"
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline(
                [executable, *args]))
            if not k.CreateProcessW(executable, command, None, None, False,
                                    CREATE_SUSPENDED, None,
                                    str(Path(executable).resolve().parent),
                                    ctypes.byref(startup), ctypes.byref(worker)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not k.AssignProcessToJobObject(job, worker.hProcess):
                raise ctypes.WinError(ctypes.get_last_error())
            if k.ResumeThread(worker.hThread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            if k.WaitForSingleObject(worker.hProcess, 7000) != WAIT_OBJECT_0:
                raise TimeoutError("Target UIA action exceeded its bound")
            if not report_file.exists():
                raise RuntimeError("Target UIA worker report is missing")
            report = json.loads(report_file.read_text(encoding="utf-8"))
            return {"performed": report.get("performed") is True,
                    "postcondition": report.get("postcondition"),
                    "target_absent_from_input_desktop":
                        _window_absent_from_input_desktop(api, hwnd, manager_thread_id),
                    "input_desktop_unchanged":
                        _input_desktop_interruption(api, expected_input_desktop,
                                                    manager_thread_id) is None}
        finally:
            if worker.hProcess:
                if k.WaitForSingleObject(worker.hProcess, 0) != WAIT_OBJECT_0:
                    k.TerminateProcess(worker.hProcess, 0xDEAD)
                    k.WaitForSingleObject(worker.hProcess, 1500)
                if worker.hThread:
                    k.CloseHandle(worker.hThread)
                k.CloseHandle(worker.hProcess)
            descriptor_file.unlink(missing_ok=True)
            report_file.unlink(missing_ok=True)

    def _invoke(self, api, hwnd: int, job, desktop_name: str,
                expected_input_desktop: str,
                manager_thread_id: int | None = None) -> dict:
        fd, report_name = tempfile.mkstemp(prefix="AIVDDS_UIA_", suffix=".json")
        os.close(fd)
        report_path = Path(report_name)
        uia_process = self._process_info_type()
        u, k = api.user32, api.kernel32
        foreground_before = int(u.GetForegroundWindow() or 0)
        try:
            interruption = _input_desktop_interruption(api, expected_input_desktop,
                                                        manager_thread_id)
            if interruption:
                return {"passed": False, **interruption}
            executable, args = api.uia_worker_command(hwnd, str(report_path))
            startup = self._startup_info_type()
            startup.cb = ctypes.sizeof(startup)
            startup.lpDesktop = f"WinSta0\\{desktop_name}"
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline([executable, *args]))
            if not k.CreateProcessW(executable, command, None, None, False, CREATE_SUSPENDED,
                                    None, str(Path(executable).resolve().parent),
                                    ctypes.byref(startup), ctypes.byref(uia_process)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not k.AssignProcessToJobObject(job, uia_process.hProcess):
                raise ctypes.WinError(ctypes.get_last_error())
            if k.ResumeThread(uia_process.hThread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            wait = k.WaitForSingleObject(uia_process.hProcess, 7000)
            if wait != WAIT_OBJECT_0:
                k.TerminateProcess(uia_process.hProcess, 0xDEAD)
                k.WaitForSingleObject(uia_process.hProcess, 1500)
                raise TimeoutError("UIA helper exceeded its seven-second bound")
            if not report_path.exists():
                raise RuntimeError("UIA helper did not return a report")
            report = json.loads(report_path.read_text(encoding="utf-8"))
            result = {"passed": report.get("passed") is True,
                      "window_found": report.get("window_found") is True,
                      "button_found": report.get("button_found") is True,
                      "button_invoked": report.get("button_invoked") is True,
                      "status_verified": report.get("status_verified") is True,
                      "controls": report.get("control_summary", []),
                      "host_foreground_unchanged": foreground_before != 0 and
                          int(u.GetForegroundWindow() or 0) == foreground_before,
                      "target_absent_from_input_desktop":
                          _window_absent_from_input_desktop(api, hwnd, manager_thread_id),
                      "input_desktop_unchanged":
                          _input_desktop_interruption(api, expected_input_desktop,
                                                      manager_thread_id) is None}
            # The foreground HWND can change because the user independently
            # changes apps during QA. The safety gate is that our exact target
            # stays off the input desktop and that desktop remains unchanged.
            result["passed"] = result["passed"] and result["target_absent_from_input_desktop"]
            result["passed"] = result["passed"] and result["input_desktop_unchanged"]
            return result
        finally:
            if uia_process.hProcess:
                if k.WaitForSingleObject(uia_process.hProcess, 0) != WAIT_OBJECT_0:
                    k.TerminateProcess(uia_process.hProcess, 0xDEAD)
                    k.WaitForSingleObject(uia_process.hProcess, 1500)
                if uia_process.hThread:
                    k.CloseHandle(uia_process.hThread)
                k.CloseHandle(uia_process.hProcess)
            report_path.unlink(missing_ok=True)

