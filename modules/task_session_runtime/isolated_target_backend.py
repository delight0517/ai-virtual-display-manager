"""Descriptor-bound adapter for manager-owned isolated target sessions.

The OS-specific session factory is injected and is deliberately absent from the
production composition until it can prove the private-desktop lifecycle. Requests
can choose a registered app ID but can never provide an executable path or UIA
control data.
"""
from __future__ import annotations

import json
import ntpath
import shutil
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from core.private_capture_codes import PRIVATE_DESKTOP_CAPTURE_DIAGNOSTIC_CODES
from core.png_capture import MAX_PNG_BYTES, PNG_SIGNATURE, validate_encoded_png
from modules.task_session_runtime.module import (
    BackendOperationError, BackendStartError, Observation, OutputRequest,
    StopReceipt, TargetHandle, TargetSpec, verify_target_executable_identity,
)

BACKEND_ID = "manager_descriptor_private_desktop"
MAX_ACCESSIBILITY_BYTES = 16 * 1024
TASK_PROFILE_TOKEN = "{VDM_TASK_PROFILE}"
_DEFAULT_TYPES = SimpleNamespace(
    BackendOperationError=BackendOperationError, BackendStartError=BackendStartError,
    Observation=Observation, OutputRequest=OutputRequest, StopReceipt=StopReceipt,
    TargetHandle=TargetHandle, TargetSpec=TargetSpec,
    verify_target_executable_identity=verify_target_executable_identity,
)


def create_native_private_desktop_session(target: TargetSpec, session_id: str,
                                          *, timeout_seconds: int,
                                          session_creator: Callable | None = None,
                                          runtime_types=None):
    """Create a descriptor-pinned WinSta0 session; input-desktop denial is fatal.

    This factory deliberately does not allocate outputs or add a target to the
    product allowlist. The task runtime must hold a verified manager output
    lease before calling it, and the host input-desktop preflight must succeed.
    """
    types = runtime_types or _DEFAULT_TYPES
    if (not isinstance(target, types.TargetSpec) or target.launch is None
            or not isinstance(session_id, str) or not session_id
            or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60):
        raise types.BackendStartError("TARGET_DESCRIPTOR_INVALID", target_created=False)
    verified = types.verify_target_executable_identity(target.launch)
    if verified.get("verified") is not True:
        raise types.BackendStartError(verified.get("error_code", "TARGET_IMAGE_UNAVAILABLE"),
                                      target_created=False)
    if session_creator is not None:
        return session_creator(target.launch, session_id)
    from modules.private_desktop.module import (
        _ExtendedLimitInformation, _ProcessInformation, _StartupInfo, _Win32,
    )
    from modules.private_desktop.session import DiagnosticSession

    try:
        api = _Win32()
    except OSError as exc:
        raise types.BackendStartError("NATIVE_PRIVATE_DESKTOP_UNAVAILABLE",
                                      target_created=False) from exc
    return DiagnosticSession(
        api, _StartupInfo, _ProcessInformation, _ExtendedLimitInformation,
        target_descriptor=target.launch, session_id=session_id)


@dataclass(frozen=True)
class _SessionBinding:
    session: object
    target: TargetSpec
    session_id: str
    identity: dict
    started_at: float
    profile_dir: Path | None = None


class DescriptorPrivateDesktopBackend:
    """Adapt a trusted target descriptor to a bounded isolated-session factory.

    `session_factory(target, session_id, timeout_seconds=...)` must create only
    the exact descriptor-pinned image on a fresh private desktop and return an
    object implementing `identity()`, `observe(timeout_seconds)`,
    `invoke(action_descriptor, timeout_seconds)`, and `stop(timeout_seconds)`.
    The factory must recheck the executable image immediately before process
    creation and return a complete cleanup receipt if it partially starts.

    This adapter is not a sandbox and is not safe to register until a concrete
    Windows factory passes native isolation/capture/control/cleanup QA.
    """

    def __init__(self, session_factory: Callable | None = None,
                 *, clock: Callable[[], float] | None = None,
                 runtime_types=None, temp_root: Path | None = None):
        if session_factory is not None and not callable(session_factory):
            raise TypeError("The private-desktop session factory must be callable")
        self._session_factory = session_factory
        self._clock = clock or time.monotonic
        self._types = runtime_types or _DEFAULT_TYPES
        self._temp_root = Path(temp_root or tempfile.gettempdir()).resolve()

    @staticmethod
    def _safe_session_id(value):
        return (isinstance(value, str) and 1 <= len(value) <= 128
                and "\x00" not in value)

    @staticmethod
    def _identity_proof(target: TargetSpec, identity):
        descriptor = target.launch
        if not isinstance(identity, dict):
            return None
        process_id, window_id = identity.get("process_id"), identity.get("window_id")
        if (type(process_id) is not int or process_id <= 0
                or type(window_id) is not int or window_id <= 0
                or identity.get("job_owned") is not True
                or identity.get("window_absent_from_input_desktop") is not True
                or identity.get("input_desktop_unchanged") is not True):
            return None
        try:
            expected_path = ntpath.normcase(ntpath.abspath(
                str(Path(descriptor.executable_path).resolve(strict=True))))
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
        actual_path = identity.get("executable_path")
        actual_hash = identity.get("executable_sha256")
        if (not isinstance(actual_path, str)
                or ntpath.normcase(ntpath.abspath(actual_path)) != expected_path
                or actual_hash != descriptor.executable_sha256):
            return None
        return process_id, window_id

    def _stop_receipt(self, value, *, profile_data_removed=True):
        receipt_type = self._types.StopReceipt
        if isinstance(value, receipt_type):
            return value
        if not isinstance(value, dict):
            return receipt_type(False, False, False, False, False, False,
                                profile_data_removed=False)
        return receipt_type(
            stopped=value.get("stopped") is True,
            target_stopped=value.get("target_stopped") is True,
            job_closed=(value.get("job_closed") is True
                        if "job_closed" in value else value.get("job_handle_closed") is True),
            desktop_removed=value.get("desktop_removed") is True,
            input_desktop_unchanged=value.get("input_desktop_unchanged") is True,
            # The dispatcher owns the separate output lease and releases it
            # only after this exact target cleanup receipt has verified.
            output_lease_released=True,
            profile_data_removed=profile_data_removed,
        )

    def _binding(self, handle):
        if (not isinstance(handle, self._types.TargetHandle) or handle.backend != BACKEND_ID
                or not isinstance(handle.opaque, _SessionBinding)):
            raise self._types.BackendOperationError(
                "ISOLATED_TARGET_HANDLE_INVALID", interrupt_reason="target_lost")
        return handle.opaque

    def _prepare_target(self, target):
        if not any(TASK_PROFILE_TOKEN in arg for arg in target.launch.arguments):
            return target, None
        try:
            profile_dir = Path(tempfile.mkdtemp(
                prefix="aivdm-onlychat-qa-", dir=str(self._temp_root))).resolve(strict=True)
            if (profile_dir.parent != self._temp_root or profile_dir.is_symlink()
                    or not profile_dir.name.startswith("aivdm-onlychat-qa-")):
                raise OSError("Task profile path failed its ownership check")
            arguments = tuple(arg.replace(TASK_PROFILE_TOKEN, str(profile_dir))
                              for arg in target.launch.arguments)
            descriptor = replace(target.launch, arguments=arguments)
            return replace(target, launch=descriptor), profile_dir
        except Exception as exc:
            raise self._types.BackendStartError(
                "TASK_PROFILE_UNAVAILABLE", target_created=False) from exc

    def _remove_profile(self, profile_dir):
        if profile_dir is None:
            return True
        try:
            candidate = Path(profile_dir)
            resolved = candidate.resolve(strict=True)
            if (resolved.parent != self._temp_root
                    or not resolved.name.startswith("aivdm-onlychat-qa-")
                    or candidate.is_symlink()):
                return False
            shutil.rmtree(resolved)
            return not resolved.exists()
        except FileNotFoundError:
            return True
        except OSError:
            return False

    def start(self, target: TargetSpec, session_id: str, *,
              output_request: OutputRequest | None = None,
              timeout_seconds: int) -> TargetHandle:
        types = self._types
        if self._session_factory is None:
            raise types.BackendStartError("ISOLATED_BACKEND_UNAVAILABLE", target_created=False)
        if (not isinstance(target, types.TargetSpec) or target.launch is None
                or set(target.action_ids) != set(target.launch.action_ids)
                or not self._safe_session_id(session_id)
                or not isinstance(output_request, types.OutputRequest)
                or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60):
            raise types.BackendStartError("TARGET_DESCRIPTOR_INVALID", target_created=False)
        launch_target, profile_dir = self._prepare_target(target)
        verified = types.verify_target_executable_identity(launch_target.launch)
        if verified.get("verified") is not True:
            self._remove_profile(profile_dir)
            raise types.BackendStartError(
                verified.get("error_code", "TARGET_IMAGE_UNAVAILABLE"),
                target_created=False)
        try:
            session = self._session_factory(launch_target, session_id,
                                            timeout_seconds=timeout_seconds)
        except types.BackendStartError as exc:
            if exc.target_created is False:
                self._remove_profile(profile_dir)
            raise
        except Exception as exc:
            from modules.private_desktop.session import (
                DiagnosticSessionStartFailure, startup_resources_absent_verified,
            )
            if not isinstance(exc, DiagnosticSessionStartFailure):
                # Without a typed cleanup receipt, partial native startup is unknown.
                raise types.BackendStartError("TARGET_START_OUTCOME_UNKNOWN") from exc
            if exc.cleanup_verified:
                self._remove_profile(profile_dir)
                raise types.BackendStartError(exc.error_code, target_created=False) from exc
            session = exc.session
            thread = getattr(session, "_thread", None) if session is not None else None
            receipt = (getattr(session, "_state", {}).get("stop_receipt")
                       if session is not None else None)
            if (thread is not None and getattr(thread, "ident", None) is not None
                    and not thread.is_alive()
                    and startup_resources_absent_verified(receipt)):
                # A typed preflight denial can leave input-desktop identity
                # unverified while proving that no owned desktop, job, process,
                # or window was ever created. Release only this empty startup;
                # never turn it into an isolation or launch success.
                self._remove_profile(profile_dir)
                raise types.BackendStartError(exc.error_code, target_created=False) from exc
            if (thread is not None and getattr(thread, "ident", None) is None
                    and not thread.is_alive()):
                # The owner thread failed to start, so no native resource
                # creation code could have run.
                self._remove_profile(profile_dir)
                raise types.BackendStartError(
                    "PRIVATE_DESKTOP_START_FAILED", target_created=False,
                ) from exc
            if session is None:
                raise types.BackendStartError("TARGET_START_OUTCOME_UNKNOWN") from exc
            identity = {}
            binding = _SessionBinding(session, launch_target, session_id, identity,
                                      self._clock(), profile_dir)
            handle = types.TargetHandle(binding, BACKEND_ID, 0, 0, False, False, False)
            raise types.BackendStartError(exc.error_code, target_created=True,
                                         handle=handle) from exc
        try:
            identity = session.identity()
        except Exception as exc:
            # Keep the returned owner attached so the dispatcher can retry stop.
            identity = {}
            binding = _SessionBinding(session, launch_target, session_id, identity,
                                      self._clock(), profile_dir)
            return types.TargetHandle(binding, BACKEND_ID, 0, 0, False, False, False)
        proof = self._identity_proof(launch_target, identity)
        binding = _SessionBinding(session, launch_target, session_id, dict(identity),
                                  self._clock(), profile_dir)
        if proof is None:
            process_id = identity.get("process_id") if isinstance(identity, dict) else None
            window_id = identity.get("window_id") if isinstance(identity, dict) else None
            return types.TargetHandle(
                binding, BACKEND_ID,
                process_id if type(process_id) is int and process_id > 0 else 0,
                window_id if type(window_id) is int and window_id > 0 else 0,
                False, False, False)
        return types.TargetHandle(binding, BACKEND_ID, proof[0], proof[1], True, True, True)

    def observe(self, handle: TargetHandle, *, timeout_seconds: int) -> Observation:
        binding = self._binding(handle)
        types = self._types
        if self._clock() - binding.started_at >= binding.target.launch.maximum_session_seconds:
            raise types.BackendOperationError("TARGET_SESSION_LIMIT_REACHED",
                                              interrupt_reason="target_lost")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
            raise types.BackendOperationError("TARGET_OBSERVE_TIMEOUT_INVALID",
                                              interrupt_reason="capture_failed")
        try:
            result = binding.session.observe(timeout_seconds=timeout_seconds)
        except Exception as exc:
            raise types.BackendOperationError("TARGET_OBSERVE_FAILED",
                                              interrupt_reason="capture_failed") from exc
        diagnostic_code = (result.get("error_code") if isinstance(result, dict) else None)
        if diagnostic_code in PRIVATE_DESKTOP_CAPTURE_DIAGNOSTIC_CODES:
            raise types.BackendOperationError(
                "TARGET_OBSERVE_FAILED", interrupt_reason="capture_failed",
                diagnostic_code=diagnostic_code)
        if not isinstance(result, dict) or result.get("session_state") != "active":
            raise types.BackendOperationError("TARGET_OBSERVATION_UNAVAILABLE",
                                              interrupt_reason="capture_failed")
        png = result.get("png_bytes")
        accessibility = result.get("accessibility")
        if (not isinstance(png, bytes) or not png.startswith(PNG_SIGNATURE)
                or len(png) > MAX_PNG_BYTES
                or result.get("content_verified") is not True
                or type(result.get("sampled_color_count")) is not int
                or not 3 <= result["sampled_color_count"] <= 144
                or not isinstance(accessibility, dict)):
            raise types.BackendOperationError("TARGET_CAPTURE_UNVERIFIED",
                                              interrupt_reason="capture_failed")
        try:
            validate_encoded_png(png)
            accessibility_bytes = json.dumps(accessibility, ensure_ascii=False,
                                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise types.BackendOperationError("TARGET_CAPTURE_INVALID",
                                              interrupt_reason="capture_failed") from exc
        if len(accessibility_bytes) > MAX_ACCESSIBILITY_BYTES:
            raise types.BackendOperationError("TARGET_ACCESSIBILITY_LIMIT_EXCEEDED",
                                              interrupt_reason="capture_failed")
        return types.Observation(png, dict(accessibility))

    def act(self, handle: TargetHandle, action_id: str, *, timeout_seconds: int) -> dict:
        binding = self._binding(handle)
        types = self._types
        if self._clock() - binding.started_at >= binding.target.launch.maximum_session_seconds:
            raise types.BackendOperationError("TARGET_SESSION_LIMIT_REACHED",
                                              interrupt_reason="target_lost")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
            raise types.BackendOperationError("TARGET_ACTION_TIMEOUT_INVALID",
                                              interrupt_reason="target_lost")
        action = next((item for item in binding.target.launch.actions
                       if item.action_id == action_id), None)
        if action is None:
            raise types.BackendOperationError("ACTION_NOT_ALLOWLISTED",
                                              interrupt_reason="target_lost")
        try:
            result = binding.session.invoke(action, timeout_seconds=timeout_seconds)
        except Exception as exc:
            raise types.BackendOperationError("TARGET_ACTION_FAILED",
                                              interrupt_reason="target_lost") from exc
        expected = {action.postcondition_name: action.postcondition_value}
        if (not isinstance(result, dict) or result.get("performed") is not True
                or result.get("postcondition") != expected
                or result.get("target_absent_from_input_desktop") is not True
                or result.get("input_desktop_unchanged") is not True):
            raise types.BackendOperationError("TARGET_ACTION_POSTCONDITION_FAILED",
                                              interrupt_reason="target_lost")
        return {"performed": True, "status_verified": True,
                "target_absent_from_input_desktop": True,
                "input_desktop_unchanged": True,
                "postcondition": expected}

    def stop(self, handle: TargetHandle, *, timeout_seconds: int) -> StopReceipt:
        binding = self._binding(handle)
        receipt_type = self._types.StopReceipt
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
            return receipt_type(False, False, False, False, False, False,
                                profile_data_removed=False)
        try:
            raw = binding.session.stop(timeout_seconds=timeout_seconds)
        except Exception:
            return receipt_type(False, False, False, False, False, False,
                                profile_data_removed=False)
        receipt = self._stop_receipt(raw)
        if not receipt.verified:
            return receipt
        removed = self._remove_profile(binding.profile_dir)
        return receipt_type(
            receipt.stopped, receipt.target_stopped, receipt.job_closed,
            receipt.desktop_removed, receipt.input_desktop_unchanged,
            output_lease_released=receipt.output_lease_released,
            profile_data_removed=removed)
