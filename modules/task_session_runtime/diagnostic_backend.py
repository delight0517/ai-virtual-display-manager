"""Concrete task-session backend for the manager-owned private-desktop QA window only."""
from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.paths import APP_VERSION
from core.png_capture import MAX_PNG_BYTES, PNG_SIGNATURE
from core.runtime_packet import decode_runtime_packet
from modules.task_session_runtime.module import (
    BackendOperationError, BackendStartError, Observation, OutputRequest, StopReceipt, TargetHandle,
    TargetSpec, TaskSessionRuntime,
)

DIAGNOSTIC_TARGET_ID = "manager_private_desktop_diagnostic"
DIAGNOSTIC_ACTION_ID = "invoke_test_button"
DIAGNOSTIC_TARGET = TargetSpec(DIAGNOSTIC_TARGET_ID, frozenset({DIAGNOSTIC_ACTION_ID}))
BACKEND_ID = "manager_owned_private_desktop_diagnostic"


def _require_running_packet(result: dict, report: dict, key: str) -> bytes:
    """Fail with the runtime state instead of decoding an absent packet."""
    if result.get("state") != "running" or not isinstance(result.get("packet"), bytes):
        report[key] = {"state": result.get("state"), "error_code": result.get("error_code")}
        raise RuntimeError(
            f"DIAGNOSTIC_{key.upper()}_{str(result.get('state', 'UNKNOWN')).upper()}:"
            f"{str(result.get('error_code') or 'PACKET_MISSING')[:120]}"
        )
    return result["packet"]


@dataclass(frozen=True)
class _OwnedSession:
    session: object
    identity: dict


def _create_private_desktop_session():
    """Late-bind Windows-only APIs so importing the manager remains side-effect free."""
    from modules.private_desktop.module import (
        _ExtendedLimitInformation, _ProcessInformation, _StartupInfo, _Win32,
    )
    from modules.private_desktop.session import DiagnosticSession

    return DiagnosticSession(_Win32(), _StartupInfo, _ProcessInformation,
                             _ExtendedLimitInformation)


def _interrupt_reason(result: dict, fallback: str) -> str:
    code = result.get("error_code")
    return {
        "HOST_INPUT_DESKTOP_CHANGED": "host_input_desktop_changed",
        "HOST_INPUT_DESKTOP_UNKNOWN": "host_input_desktop_unknown",
        "TARGET_EXITED": "target_lost",
    }.get(code, fallback)


class PrivateDesktopDiagnosticBackend:
    """Adapts the owned diagnostic window to the task-runtime backend protocol.

    This is an explicit QA backend. It never accepts an executable path or launches
    an application selected by a request; production defaults do not instantiate it.
    """

    def __init__(self, session_factory: Callable | None = None):
        self._session_factory = session_factory or _create_private_desktop_session

    def start(self, target: TargetSpec, session_id: str, *, output_request: OutputRequest | None = None,
              timeout_seconds: int) -> TargetHandle:
        if (not isinstance(target, TargetSpec)
                or target.app_id != DIAGNOSTIC_TARGET_ID
                or target.action_ids != DIAGNOSTIC_TARGET.action_ids
                or output_request is not None and not isinstance(output_request, OutputRequest)):
            raise BackendStartError("TARGET_APP_NOT_ALLOWLISTED", target_created=False)
        try:
            session = self._session_factory()
        except Exception as exc:
            # Only this typed receipt proves that constructor failure left no
            # target and fully removed its owned diagnostic resources. An
            # uncertain typed failure carries a stop-only handle for retry.
            # Untyped errors remain fail-closed upstream.
            from modules.private_desktop.session import DiagnosticSessionStartFailure
            from modules.private_desktop.session import startup_resources_absent_verified

            if isinstance(exc, DiagnosticSessionStartFailure):
                if exc.cleanup_verified:
                    receipt = exc.cleanup_receipt
                    raise BackendStartError(
                        exc.error_code, target_created=False,
                        cleanup_receipt=StopReceipt(
                            stopped=True,
                            target_stopped=receipt.get("target_stopped") is True,
                            job_closed=receipt.get("job_handle_closed") is True,
                            desktop_removed=receipt.get("desktop_removed") is True,
                            input_desktop_unchanged=(
                                receipt.get("input_desktop_unchanged") is True),
                        ),
                    ) from exc
                failed_session = exc.session
                thread = getattr(failed_session, "_thread", None)
                receipt = getattr(failed_session, "_state", {}).get("stop_receipt")
                if (failed_session is not None and thread is not None
                        and thread.ident is not None and not thread.is_alive()
                        and startup_resources_absent_verified(receipt)):
                    # A read-only preflight denial before CreateDesktop/job/process
                    # has no target or cleanup handle to retain. Keep input-desktop
                    # verification false when it was unreadable; this only releases
                    # the provisional target session, not the isolation gate.
                    raise BackendStartError(
                        exc.error_code, target_created=False,
                    ) from exc
                if (failed_session is not None and thread is not None
                        and getattr(thread, "ident", None) is None
                        and not thread.is_alive()):
                    # Thread startup failed before the owner could allocate resources.
                    raise BackendStartError(
                        "PRIVATE_DESKTOP_START_FAILED", target_created=False,
                    ) from exc
                if failed_session is not None:
                    partial = TargetHandle(
                        opaque=_OwnedSession(failed_session, {}), backend=BACKEND_ID,
                        process_id=0, window_id=0, job_owned=False,
                        window_absent_from_input_desktop=False,
                        input_desktop_unchanged=False,
                    )
                    raise BackendStartError(
                        exc.error_code, handle=partial,
                    ) from exc
            raise
        identity = session.identity()
        return TargetHandle(
            opaque=_OwnedSession(session, dict(identity)), backend=BACKEND_ID,
            process_id=identity.get("process_id"), window_id=identity.get("window_id"),
            job_owned=identity.get("job_owned") is True,
            window_absent_from_input_desktop=(
                identity.get("window_absent_from_input_desktop") is True),
            input_desktop_unchanged=identity.get("input_desktop_unchanged") is True,
        )

    @staticmethod
    def _session(handle: TargetHandle):
        if (not isinstance(handle, TargetHandle)
                or handle.backend != BACKEND_ID
                or not isinstance(handle.opaque, _OwnedSession)):
            raise BackendOperationError("DIAGNOSTIC_HANDLE_INVALID", interrupt_reason="target_lost")
        return handle.opaque.session

    def observe(self, handle: TargetHandle, *, timeout_seconds: int) -> Observation:
        result = self._session(handle).request("observe")
        if result.get("session_state") != "active":
            reason = _interrupt_reason(result, "capture_failed")
            raise BackendOperationError("DIAGNOSTIC_OBSERVE_FAILED", interrupt_reason=reason)
        try:
            png = base64.b64decode(result.get("png_bytes", ""), validate=True)
        except (ValueError, TypeError) as exc:
            raise BackendOperationError("DIAGNOSTIC_PNG_INVALID",
                                        interrupt_reason="capture_failed") from exc
        accessibility = {
            "window_title": str(result.get("title", ""))[:120],
            "width": result.get("width"),
            "height": result.get("height"),
            "unique_sample_colors": result.get("unique_sample_colors"),
            "window_absent_from_input_desktop": handle.window_absent_from_input_desktop,
            "input_desktop_unchanged": handle.input_desktop_unchanged,
            "input_desktop_before": handle.opaque.identity.get("input_desktop_before"),
            "input_desktop_after": handle.opaque.identity.get("input_desktop_after"),
        }
        return Observation(png, accessibility)

    def act(self, handle: TargetHandle, action_id: str, *, timeout_seconds: int) -> dict:
        if action_id != DIAGNOSTIC_ACTION_ID:
            raise BackendOperationError("ACTION_NOT_ALLOWLISTED", interrupt_reason="target_lost")
        result = self._session(handle).request("invoke")
        if result.get("passed") is not True:
            reason = _interrupt_reason(result, "target_lost")
            raise BackendOperationError("DIAGNOSTIC_ACTION_FAILED", interrupt_reason=reason)
        return {
            "performed": result.get("button_invoked") is True,
            "status_verified": result.get("status_verified") is True,
            "target_absent_from_input_desktop":
                result.get("target_absent_from_input_desktop") is True,
            "input_desktop_unchanged": result.get("input_desktop_unchanged") is True,
        }

    def stop(self, handle: TargetHandle, *, timeout_seconds: int) -> StopReceipt:
        result = self._session(handle).request("stop")
        return StopReceipt(
            stopped=result.get("stopped") is True,
            target_stopped=result.get("target_stopped") is True,
            job_closed=result.get("job_handle_closed") is True,
            desktop_removed=result.get("desktop_removed") is True,
            input_desktop_unchanged=result.get("input_desktop_unchanged") is True,
        )


def run_task_runtime_private_desktop_self_check(report_path: str) -> int:
    """Run dispatcher start/observe/act/stop using only the manager diagnostic window."""
    report = {"app_version": APP_VERSION, "check": "manager-owned-private-desktop-task-runtime",
              "scope": "manager_diagnostic_only",
              "output_allocation": "not_exercised",
              "target_id": DIAGNOSTIC_TARGET_ID, "passed": False}
    runtime = None
    session_id = None
    exit_code = 1

    def request(action: str, **fields):
        if action == "start":
            fields.setdefault("output_request", {"output_count": 2, "profile": "high"})
        return {"protocol_version": 2, "request_id": str(uuid.uuid4()),
                "action": action, **fields}

    try:
        runtime = TaskSessionRuntime(
            backend=PrivateDesktopDiagnosticBackend(),
            target_registry={DIAGNOSTIC_TARGET_ID: DIAGNOSTIC_TARGET},
        )
        runtime.init()
        started = runtime.handle_request(request(
            "start", target_app_id=DIAGNOSTIC_TARGET_ID, confirmed=True))
        session_id = started.get("session_id")
        report["start"] = {
            "state": started.get("state"),
            "error_code": started.get("error_code"),
            "backend": started.get("backend"),
            "process_id_valid": type(started.get("process_id")) is int
                and started["process_id"] > 0,
            "window_id_valid": type(started.get("window_id")) is int
                and started["window_id"] > 0,
            "isolation_proof": started.get("isolation_proof"),
            "capabilities": started.get("capabilities"),
        }
        if started.get("state") != "ready" or not session_id:
            raise RuntimeError(
                f"TASK_RUNTIME_START_FAILED:{str(started.get('error_code') or 'UNKNOWN')[:120]}")

        first = runtime.handle_request(request("observe", session_id=session_id))
        header_before, png_before = decode_runtime_packet(
            _require_running_packet(first, report, "observe_before"))
        report["observe_before"] = {
            "state": first.get("state"), "header_request_id_matches":
                header_before.get("request_id") == first.get("request_id"),
            "png_signature_valid": png_before.startswith(PNG_SIGNATURE),
            "png_size_bounded": 0 < len(png_before) <= MAX_PNG_BYTES,
            "png_sha256": header_before.get("payload_sha256"),
            "accessibility": header_before.get("accessibility"),
        }

        action = runtime.handle_request(request(
            "act", session_id=session_id, action_id=DIAGNOSTIC_ACTION_ID))
        action_result = action.get("action_result") or {}
        report["action"] = {
            "state": action.get("state"),
            "error_code": action.get("error_code"),
            **action_result,
        }
        if action.get("state") != "running":
            # An interrupted/failed action has no subsequent observation packet.
            # Preserve the actual state instead of masking it with a decode error.
            raise RuntimeError(
                f"DIAGNOSTIC_ACTION_{str(action.get('state', 'UNKNOWN')).upper()}:"
                f"{str(action.get('error_code') or 'UNKNOWN')[:120]}"
            )

        second = runtime.handle_request(request("observe", session_id=session_id))
        header_after, png_after = decode_runtime_packet(
            _require_running_packet(second, report, "observe_after"))
        report["observe_after"] = {
            "state": second.get("state"), "header_request_id_matches":
                header_after.get("request_id") == second.get("request_id"),
            "png_signature_valid": png_after.startswith(PNG_SIGNATURE),
            "png_size_bounded": 0 < len(png_after) <= MAX_PNG_BYTES,
            "png_sha256": header_after.get("payload_sha256"),
            "frame_changed_after_action":
                header_before.get("payload_sha256") != header_after.get("payload_sha256"),
            "accessibility": header_after.get("accessibility"),
        }

        stopped = runtime.handle_request(request("stop", session_id=session_id))
        report["stop"] = {
            "state": stopped.get("state"),
            "receipt": stopped.get("stop_receipt"),
        }
        report["passed"] = all((
            report["start"]["state"] == "ready",
            report["start"]["backend"] == BACKEND_ID,
            report["start"]["process_id_valid"], report["start"]["window_id_valid"],
            report["start"]["isolation_proof"] == {
                "window_absent_from_input_desktop": True,
                "input_desktop_unchanged": True,
            },
            report["observe_before"]["state"] == "running",
            report["observe_before"]["header_request_id_matches"],
            report["observe_before"]["png_signature_valid"],
            report["observe_before"]["png_size_bounded"],
            report["action"]["state"] == "running",
            report["action"].get("performed") is True,
            report["action"].get("status_verified") is True,
            report["action"].get("target_absent_from_input_desktop") is True,
            report["action"].get("input_desktop_unchanged") is True,
            report["observe_after"]["state"] == "running",
            report["observe_after"]["header_request_id_matches"],
            report["observe_after"]["png_signature_valid"],
            report["observe_after"]["png_size_bounded"],
            report["observe_after"]["frame_changed_after_action"],
            report["stop"]["state"] == "stopped",
            isinstance(report["stop"]["receipt"], dict)
                and all(report["stop"]["receipt"].get(name) is True for name in (
                    "stopped", "target_stopped", "job_closed", "desktop_removed",
                    "input_desktop_unchanged")),
        ))
        exit_code = 0 if report["passed"] else 1
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        if runtime is not None:
            try:
                if session_id and runtime.refresh().get("active_session_id") == session_id:
                    stopped = runtime.handle_request(request("stop", session_id=session_id))
                    report.setdefault("cleanup", {})["state"] = stopped.get("state")
                    report["cleanup"]["receipt"] = stopped.get("stop_receipt")
                runtime.dispose()
            except Exception as exc:
                report["cleanup_error"] = f"{type(exc).__name__}: {exc}"[:240]
                exit_code = 1
        output = Path(report_path).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return exit_code

