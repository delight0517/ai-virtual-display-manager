from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.png_capture import encode_bgra_png
from core.runtime_packet import decode_runtime_packet
from modules.task_session_runtime.diagnostic_backend import (
    BACKEND_ID, DIAGNOSTIC_ACTION_ID, DIAGNOSTIC_TARGET,
    DIAGNOSTIC_TARGET_ID, PrivateDesktopDiagnosticBackend, _require_running_packet,
)
from modules.task_session_runtime.module import TaskSessionRuntime
from modules.task_session_runtime.module import BackendStartError
from modules.private_desktop.session import (
    DiagnosticSessionStartError, DiagnosticSessionStartFailure,
)


PNG_BEFORE = encode_bgra_png(bytes((0, 0, 0, 0, 0, 0, 255, 0,
                                   0, 255, 0, 0, 255, 0, 0, 0)), 2, 2)
PNG_AFTER = encode_bgra_png(bytes((255, 255, 255, 0, 0, 0, 255, 0,
                                  0, 255, 0, 0, 255, 0, 0, 0)), 2, 2)


class FakeDiagnosticSession:
    def __init__(self, *, proof=True, observe_interrupt=None, stop_responses=None):
        self.proof = proof
        self.observe_interrupt = observe_interrupt
        self.stop_responses = list(stop_responses or [])
        self.stop_count = 0
        self.observation_count = 0
        self.actions = []
        self.stopped = False

    def identity(self):
        return {
            "process_id": 2001, "window_id": 3001, "job_owned": True,
            "window_absent_from_input_desktop": self.proof,
            "input_desktop_unchanged": True,
            "input_desktop_before": "Default", "input_desktop_after": "Default",
        }

    def request(self, action):
        if action == "observe":
            if self.observe_interrupt:
                return {"session_state": "interrupted",
                        "error_code": self.observe_interrupt}
            self.observation_count += 1
            png = PNG_BEFORE if self.observation_count == 1 else PNG_AFTER
            import base64
            return {"session_state": "active", "png_bytes": base64.b64encode(png).decode(),
                    "title": "Owned diagnostic", "width": 2, "height": 2,
                    "unique_sample_colors": 4}
        if action == "invoke":
            self.actions.append(action)
            return {"passed": True, "button_invoked": True, "status_verified": True,
                    "target_absent_from_input_desktop": self.proof,
                    "input_desktop_unchanged": True}
        if action == "stop":
            self.stop_count += 1
            result = (self.stop_responses.pop(0) if self.stop_responses else {
                "stopped": True, "target_stopped": True,
                "job_handle_closed": True, "desktop_removed": True,
                "input_desktop_unchanged": True,
            })
            self.stopped = result.get("stopped") is True
            return result
        raise AssertionError(f"unexpected diagnostic action: {action}")


def request(action, **fields):
    if action == "start":
        fields.setdefault("output_request", {"output_count": 2, "profile": "high"})
    return {"protocol_version": 2, "request_id": str(uuid.uuid4()),
            "action": action, **fields}


class TaskSessionDiagnosticBackendTests(unittest.TestCase):
    def test_preflight_denial_without_created_resources_releases_only_provisional_session(self):
        import threading

        session = FakeDiagnosticSession()
        worker = threading.Thread(target=lambda: None)
        worker.start()
        worker.join(timeout=2)
        session._thread = worker
        session._state = {"stop_receipt": {
            "stopped": False, "input_desktop_unchanged": False,
            "desktop_created": False, "job_created": False,
            "process_created": False, "window_created": False,
        }}

        def deny_preflight():
            raise DiagnosticSessionStartFailure(
                "OpenInputDesktop unavailable", session=session,
                cleanup_receipt=session._state["stop_receipt"],
                error_code="HOST_INPUT_DESKTOP_ACCESS_DENIED",
            )

        runtime = TaskSessionRuntime(
            backend=PrivateDesktopDiagnosticBackend(deny_preflight),
            target_registry={DIAGNOSTIC_TARGET_ID: DIAGNOSTIC_TARGET},
            watchdog_interval=0,
        )
        runtime.init()
        try:
            result = runtime.handle_request(request(
                "start", target_app_id=DIAGNOSTIC_TARGET_ID, confirmed=True))
            self.assertEqual(result["error_code"], "HOST_INPUT_DESKTOP_ACCESS_DENIED")
            self.assertIsNone(runtime.refresh()["active_session_id"])
            self.assertFalse(session._state["stop_receipt"]["input_desktop_unchanged"])
            self.assertEqual(session.stop_count, 0)
        finally:
            runtime.dispose()

    def test_diagnostic_report_preserves_interrupted_state_without_decoding_missing_packet(self):
        report = {}
        with self.assertRaisesRegex(RuntimeError, "DIAGNOSTIC_OBSERVE_AFTER_INTERRUPTED:TARGET_LOST"):
            _require_running_packet(
                {"state": "interrupted", "error_code": "TARGET_LOST"},
                report, "observe_after")
        self.assertEqual(report["observe_after"], {
            "state": "interrupted", "error_code": "TARGET_LOST"})

    def make_runtime(self, session):
        return TaskSessionRuntime(
            backend=PrivateDesktopDiagnosticBackend(lambda: session),
            target_registry={DIAGNOSTIC_TARGET_ID: DIAGNOSTIC_TARGET},
            watchdog_interval=0,
        )

    def test_dispatcher_runs_owned_start_observe_action_observe_and_verified_stop(self):
        session = FakeDiagnosticSession()
        runtime = self.make_runtime(session)
        runtime.init()
        try:
            started = runtime.handle_request(request(
                "start", target_app_id=DIAGNOSTIC_TARGET_ID, confirmed=True))
            session_id = started["session_id"]
            self.assertEqual(started["state"], "ready")
            self.assertEqual(started["backend"], BACKEND_ID)
            self.assertEqual(started["isolation_proof"], {
                "window_absent_from_input_desktop": True,
                "input_desktop_unchanged": True,
            })

            before = runtime.handle_request(request("observe", session_id=session_id))
            before_header, before_png = decode_runtime_packet(before["packet"])
            self.assertEqual(before["state"], "running")
            self.assertEqual(before_header["accessibility"]["input_desktop_before"], "Default")
            self.assertEqual(before_png, PNG_BEFORE)

            action = runtime.handle_request(request(
                "act", session_id=session_id, action_id=DIAGNOSTIC_ACTION_ID))
            self.assertEqual(action["state"], "running")
            self.assertTrue(action["action_result"]["performed"])
            self.assertEqual(session.actions, ["invoke"])

            after = runtime.handle_request(request("observe", session_id=session_id))
            after_header, after_png = decode_runtime_packet(after["packet"])
            self.assertEqual(after["state"], "running")
            self.assertNotEqual(before_header["payload_sha256"],
                                after_header["payload_sha256"])
            self.assertEqual(after_png, PNG_AFTER)

            stopped = runtime.handle_request(request("stop", session_id=session_id))
            self.assertEqual(stopped["state"], "stopped")
            self.assertTrue(all(stopped["stop_receipt"].values()))
            self.assertTrue(session.stopped)
        finally:
            runtime.dispose()

    def test_manager_diagnostic_backend_rejects_every_other_target(self):
        constructed = []
        backend = PrivateDesktopDiagnosticBackend(lambda: constructed.append(True))
        with self.assertRaises(BackendStartError):
            backend.start(type("Spec", (), {"app_id": "blender", "action_ids": frozenset()})(),
                          str(uuid.uuid4()), timeout_seconds=15)
        self.assertEqual(constructed, [])

    def test_verified_clean_constructor_failure_is_reported_as_target_absent(self):
        receipt = {
            "stopped": True, "target_stopped": True,
            "desktop_thread_restored": True, "desktop_handle_closed": True,
            "desktop_removed": True, "owned_window_removed": True,
            "job_handle_closed": True,
            "input_desktop_unchanged": True,
        }

        def fail_cleanly():
            raise DiagnosticSessionStartError("OpenDesktop failed", receipt)

        backend = PrivateDesktopDiagnosticBackend(fail_cleanly)
        with self.assertRaises(BackendStartError) as caught:
            backend.start(DIAGNOSTIC_TARGET, str(uuid.uuid4()), timeout_seconds=15)
        self.assertIs(caught.exception.target_created, False)
        self.assertTrue(caught.exception.cleanup_receipt.verified)

    def test_untyped_constructor_failure_stays_fail_closed(self):
        def fail_uncertainly():
            raise RuntimeError("unknown startup/cleanup state")

        runtime = TaskSessionRuntime(
            backend=PrivateDesktopDiagnosticBackend(fail_uncertainly),
            target_registry={DIAGNOSTIC_TARGET_ID: DIAGNOSTIC_TARGET},
            watchdog_interval=0,
        )
        runtime.init()
        result = runtime.handle_request(request(
            "start", target_app_id=DIAGNOSTIC_TARGET_ID, confirmed=True))
        self.assertEqual(result["error_code"], "START_CLEANUP_UNVERIFIED")
        self.assertIsNotNone(runtime.refresh()["active_session_id"])
        with self.assertRaisesRegex(RuntimeError, "cleanup not verified"):
            runtime.dispose()
        self.assertTrue(runtime._initialized)

    def test_verified_clean_constructor_failure_releases_provisional_session(self):
        receipt = {
            "stopped": True, "target_stopped": True,
            "desktop_thread_restored": True, "desktop_handle_closed": True,
            "desktop_removed": True, "owned_window_removed": True,
            "job_handle_closed": True,
            "input_desktop_unchanged": True,
        }

        def fail_cleanly():
            raise DiagnosticSessionStartError("OpenDesktop failed", receipt)

        runtime = TaskSessionRuntime(
            backend=PrivateDesktopDiagnosticBackend(fail_cleanly),
            target_registry={DIAGNOSTIC_TARGET_ID: DIAGNOSTIC_TARGET},
            watchdog_interval=0,
        )
        runtime.init()
        try:
            result = runtime.handle_request(request(
                "start", target_app_id=DIAGNOSTIC_TARGET_ID, confirmed=True))
            self.assertEqual(result["error_code"], "PRIVATE_DESKTOP_START_FAILED")
            self.assertIsNone(runtime.refresh()["active_session_id"])
        finally:
            runtime.dispose()

    def test_uncertain_constructor_failure_keeps_partial_handle_for_stop_retry(self):
        incomplete = {
            "stopped": False, "target_stopped": False,
            "job_handle_closed": False, "desktop_removed": False,
            "input_desktop_unchanged": False,
        }
        complete = {
            "stopped": True, "target_stopped": True,
            "job_handle_closed": True, "desktop_removed": True,
            "input_desktop_unchanged": True,
        }
        session = FakeDiagnosticSession(stop_responses=[incomplete, complete])

        def fail_with_owned_session():
            raise DiagnosticSessionStartFailure(
                "startup cleanup still pending", session=session,
                cleanup_receipt=incomplete,
            )

        runtime = TaskSessionRuntime(
            backend=PrivateDesktopDiagnosticBackend(fail_with_owned_session),
            target_registry={DIAGNOSTIC_TARGET_ID: DIAGNOSTIC_TARGET},
            watchdog_interval=0,
        )
        runtime.init()
        try:
            started = runtime.handle_request(request(
                "start", target_app_id=DIAGNOSTIC_TARGET_ID, confirmed=True))
            self.assertEqual(started["error_code"], "START_CLEANUP_UNVERIFIED")
            self.assertEqual(session.stop_count, 1)
            self.assertEqual(runtime.refresh()["active_session_id"], started["session_id"])

            retried = runtime.handle_request(request("stop", session_id=started["session_id"]))
            self.assertEqual(retried["state"], "stopped")
            self.assertEqual(session.stop_count, 2)
            self.assertIsNone(runtime.refresh()["active_session_id"])
        finally:
            runtime.dispose()

    def test_unverified_isolation_is_stopped_before_dispatcher_refuses_ready(self):
        session = FakeDiagnosticSession(proof=False)
        runtime = self.make_runtime(session)
        runtime.init()
        try:
            result = runtime.handle_request(request(
                "start", target_app_id=DIAGNOSTIC_TARGET_ID, confirmed=True))
            self.assertEqual(result["error_code"], "TARGET_ISOLATION_UNVERIFIED")
            self.assertTrue(session.stopped)
            self.assertIsNone(runtime.refresh()["active_session_id"])
        finally:
            runtime.dispose()

    def test_secure_desktop_observation_error_keeps_its_specific_interrupt_reason(self):
        session = FakeDiagnosticSession(observe_interrupt="HOST_INPUT_DESKTOP_CHANGED")
        runtime = self.make_runtime(session)
        runtime.init()
        try:
            started = runtime.handle_request(request(
                "start", target_app_id=DIAGNOSTIC_TARGET_ID, confirmed=True))
            result = runtime.handle_request(request("observe", session_id=started["session_id"]))
            self.assertEqual(result["state"], "interrupted")
            self.assertEqual(result["error_code"], "host_input_desktop_changed")
            stopped = runtime.handle_request(request(
                "stop", session_id=started["session_id"]))
            self.assertEqual(stopped["state"], "stopped")
        finally:
            runtime.dispose()


if __name__ == "__main__":
    unittest.main(verbosity=2)

