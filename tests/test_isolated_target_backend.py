from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.png_capture import encode_bgra_png
from core.runtime_packet import decode_runtime_packet
from modules.task_session_runtime.isolated_target_backend import (
    BACKEND_ID, DescriptorPrivateDesktopBackend,
    create_native_private_desktop_session,
)
from modules.task_session_runtime.module import (
    BackendOperationError, BackendStartError, OutputRequest, TargetActionDescriptor,
    TargetLaunchDescriptor, TargetSpec, TaskSessionRuntime,
)
from modules.private_desktop.session import (
    DiagnosticSessionStartError, DiagnosticSessionStartFailure,
)


PNG = encode_bgra_png(bytes((0, 0, 0, 0, 0, 0, 255, 0,
                             0, 255, 0, 0, 255, 0, 0, 0)), 2, 2)


class FakeIsolatedSession:
    def __init__(self, descriptor, *, identity_overrides=None, observation=None,
                 action=None, stop=None):
        self.descriptor = descriptor
        self.identity_overrides = dict(identity_overrides or {})
        self.observation = observation
        self.action = action
        self.stop_result = stop or {
            "stopped": True, "target_stopped": True, "job_closed": True,
            "desktop_removed": True, "input_desktop_unchanged": True,
        }
        self.invoked = []
        self.stop_calls = 0

    def identity(self):
        result = {
            "process_id": 2201, "window_id": 3301, "job_owned": True,
            "window_absent_from_input_desktop": True,
            "input_desktop_unchanged": True,
            "executable_path": str(Path(self.descriptor.executable_path).resolve()),
            "executable_sha256": self.descriptor.executable_sha256,
        }
        result.update(self.identity_overrides)
        return result

    def observe(self, *, timeout_seconds):
        if self.observation is not None:
            return self.observation
        return {"session_state": "active", "png_bytes": PNG,
                "content_verified": True, "sampled_color_count": 4,
                "accessibility": {"window": "Owned target", "state": "ready"}}

    def invoke(self, action, *, timeout_seconds):
        self.invoked.append(action)
        if self.action is not None:
            return self.action
        return {"performed": True,
                "postcondition": {action.postcondition_name: action.postcondition_value},
                "target_absent_from_input_desktop": True,
                "input_desktop_unchanged": True}

    def stop(self, *, timeout_seconds):
        self.stop_calls += 1
        return self.stop_result


class DescriptorPrivateDesktopBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "owned-target.exe"
        path.write_bytes(b"disposable test image identity")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.action = TargetActionDescriptor(
            "open_panel", "Button", "Open panel", "panel_state", "open")
        self.descriptor = TargetLaunchDescriptor(
            executable_path=str(path), executable_sha256=digest, arguments=(),
            working_directory=self.temp.name, window_class_names=("OwnedWindow",),
            actions=(self.action,), maximum_session_seconds=3600)
        self.target = TargetSpec("demo.target", frozenset({"open_panel"}),
                                 self.descriptor)
        self.output_request = OutputRequest(2, "high")

    def tearDown(self):
        self.temp.cleanup()

    def make_session(self, **kwargs):
        return FakeIsolatedSession(self.descriptor, **kwargs)

    def make_backend(self, session):
        calls = []
        def factory(target, session_id, *, timeout_seconds):
            calls.append((target.app_id, session_id, timeout_seconds))
            return session
        return DescriptorPrivateDesktopBackend(factory), calls

    def start(self, backend, target=None):
        return backend.start(target or self.target, "task-session-1",
                             output_request=self.output_request, timeout_seconds=15)

    def test_absent_factory_fails_closed_without_claiming_target_creation(self):
        backend = DescriptorPrivateDesktopBackend()
        with self.assertRaises(BackendStartError) as caught:
            self.start(backend)
        self.assertEqual(caught.exception.error_code, "ISOLATED_BACKEND_UNAVAILABLE")
        self.assertIs(caught.exception.target_created, False)

    def test_native_factory_forwards_only_verified_registered_descriptor(self):
        created = []
        session = object()
        result = create_native_private_desktop_session(
            self.target, "session-native-test", timeout_seconds=15,
            session_creator=lambda descriptor, session_id: (
                created.append((descriptor, session_id)) or session))
        self.assertIs(result, session)
        self.assertEqual(created, [(self.descriptor, "session-native-test")])

    def test_native_factory_rejects_unverified_image_before_session_creation(self):
        Path(self.descriptor.executable_path).write_bytes(b"changed image")
        created = []
        with self.assertRaises(BackendStartError) as caught:
            create_native_private_desktop_session(
                self.target, "session-native-test", timeout_seconds=15,
                session_creator=lambda *args: created.append(args))
        self.assertEqual(caught.exception.error_code, "TARGET_IMAGE_HASH_MISMATCH")
        self.assertEqual(created, [])

    def test_typed_start_failure_without_target_receipt_is_propagated_as_absent(self):
        cleanup = {key: True for key in (
            "stopped", "target_stopped", "desktop_thread_restored",
            "desktop_handle_closed", "desktop_removed", "owned_window_removed",
            "job_handle_closed", "input_desktop_unchanged")}
        failure = DiagnosticSessionStartError(
            "input desktop access denied", cleanup,
            error_code="HOST_INPUT_DESKTOP_ACCESS_DENIED")
        backend = DescriptorPrivateDesktopBackend(
            lambda *args, **kwargs: (_ for _ in ()).throw(failure))
        with self.assertRaises(BackendStartError) as caught:
            self.start(backend)
        self.assertEqual(caught.exception.error_code, "HOST_INPUT_DESKTOP_ACCESS_DENIED")
        self.assertIs(caught.exception.target_created, False)
        self.assertIsNone(caught.exception.handle)

    def test_preflight_failure_with_exited_worker_and_no_resources_releases_empty_start(self):
        session = self.make_session()
        session._thread = type("ExitedThread", (), {
            "ident": 123, "is_alive": lambda _self: False,
        })()
        receipt = {"desktop_created": False, "job_created": False,
                   "process_created": False, "window_created": False,
                   "input_desktop_unchanged": False}
        session._state = {"stop_receipt": receipt}
        failure = DiagnosticSessionStartFailure(
            "host input desktop could not be verified", session=session,
            cleanup_receipt=receipt, error_code="HOST_INPUT_DESKTOP_UNKNOWN")
        backend = DescriptorPrivateDesktopBackend(
            lambda *args, **kwargs: (_ for _ in ()).throw(failure))

        with self.assertRaises(BackendStartError) as caught:
            self.start(backend)

        self.assertEqual(caught.exception.error_code, "HOST_INPUT_DESKTOP_UNKNOWN")
        self.assertIs(caught.exception.target_created, False)
        self.assertIsNone(caught.exception.handle)

    def test_exited_worker_requires_explicit_false_for_every_resource_receipt_field(self):
        fields = ("desktop_created", "job_created", "process_created", "window_created")
        invalid_values = (None, 0, 0.0, "", [], {})
        for field in fields:
            for invalid in invalid_values:
                with self.subTest(field=field, invalid=repr(invalid)):
                    session = self.make_session()
                    session._thread = type("ExitedThread", (), {
                        "ident": 123, "is_alive": lambda _self: False,
                    })()
                    receipt = {name: False for name in fields}
                    receipt[field] = invalid
                    session._state = {"stop_receipt": receipt}
                    failure = DiagnosticSessionStartFailure(
                        "incomplete startup resource receipt", session=session,
                        cleanup_receipt=receipt, error_code="HOST_INPUT_DESKTOP_UNKNOWN")
                    backend = DescriptorPrivateDesktopBackend(
                        lambda *args, **kwargs: (_ for _ in ()).throw(failure))

                    with self.assertRaises(BackendStartError) as caught:
                        self.start(backend)

                    self.assertIs(caught.exception.target_created, True)
                    self.assertIsNotNone(caught.exception.handle)
                    self.assertFalse(caught.exception.handle.ready_verified)

    def test_empty_receipt_with_live_startup_worker_remains_stop_only(self):
        session = self.make_session()
        session._thread = type("LiveThread", (), {
            "ident": 123, "is_alive": lambda _self: True,
        })()
        receipt = {"desktop_created": False, "job_created": False,
                   "process_created": False, "window_created": False}
        session._state = {"stop_receipt": receipt}
        failure = DiagnosticSessionStartFailure(
            "startup still cleaning", session=session, cleanup_receipt=receipt,
            error_code="HOST_INPUT_DESKTOP_UNKNOWN")
        backend = DescriptorPrivateDesktopBackend(
            lambda *args, **kwargs: (_ for _ in ()).throw(failure))

        with self.assertRaises(BackendStartError) as caught:
            self.start(backend)

        self.assertIs(caught.exception.target_created, True)
        self.assertIsNotNone(caught.exception.handle)
        self.assertFalse(caught.exception.handle.ready_verified)

    def test_uncertain_typed_start_retains_retryable_stop_only_handle(self):
        session = self.make_session()
        failure = DiagnosticSessionStartFailure(
            "cleanup pending", session=session,
            cleanup_receipt={"stopped": False})
        backend = DescriptorPrivateDesktopBackend(
            lambda *args, **kwargs: (_ for _ in ()).throw(failure))
        with self.assertRaises(BackendStartError) as caught:
            self.start(backend)
        handle = caught.exception.handle
        self.assertIsNotNone(handle)
        self.assertFalse(handle.ready_verified)
        self.assertIs(caught.exception.target_created, True)
        receipt = backend.stop(handle, timeout_seconds=5)
        self.assertTrue(receipt.verified)
        self.assertEqual(session.stop_calls, 1)

    def test_unlaunchable_or_unregistered_target_never_reaches_factory(self):
        called = []
        backend = DescriptorPrivateDesktopBackend(lambda *a, **k: called.append(True))
        unlaunchable = TargetSpec("demo.target", frozenset({"open_panel"}))
        with self.assertRaises(BackendStartError) as caught:
            self.start(backend, unlaunchable)
        self.assertEqual(caught.exception.error_code, "TARGET_DESCRIPTOR_INVALID")
        self.assertEqual(called, [])

    def test_executable_hash_is_rechecked_before_factory(self):
        Path(self.descriptor.executable_path).write_bytes(b"changed after allowlist")
        called = []
        backend = DescriptorPrivateDesktopBackend(lambda *a, **k: called.append(True))
        with self.assertRaises(BackendStartError) as caught:
            self.start(backend)
        self.assertEqual(caught.exception.error_code, "TARGET_IMAGE_HASH_MISMATCH")
        self.assertEqual(called, [])

    def test_valid_private_target_lifecycle_returns_observation_postcondition_and_cleanup(self):
        session = self.make_session()
        backend, calls = self.make_backend(session)
        handle = self.start(backend)
        self.assertTrue(handle.ready_verified)
        self.assertEqual(handle.backend, BACKEND_ID)
        self.assertEqual(calls, [("demo.target", "task-session-1", 15)])
        observation = backend.observe(handle, timeout_seconds=10)
        self.assertEqual(observation.png, PNG)
        self.assertEqual(observation.accessibility["state"], "ready")
        result = backend.act(handle, "open_panel", timeout_seconds=10)
        self.assertTrue(result["status_verified"])
        self.assertEqual(session.invoked, [self.action])
        receipt = backend.stop(handle, timeout_seconds=5)
        self.assertTrue(receipt.verified)

    def test_identity_mismatch_returns_stop_only_handle(self):
        session = self.make_session(identity_overrides={"executable_sha256": "0" * 64})
        backend, _ = self.make_backend(session)
        handle = self.start(backend)
        self.assertFalse(handle.ready_verified)
        receipt = backend.stop(handle, timeout_seconds=5)
        self.assertTrue(receipt.verified)
        self.assertEqual(session.stop_calls, 1)

    def test_identity_never_passes_through_invalid_pid_or_hwnd_types(self):
        session = self.make_session(identity_overrides={"process_id": "2201", "window_id": True})
        backend, _ = self.make_backend(session)
        handle = self.start(backend)
        self.assertFalse(handle.ready_verified)
        self.assertEqual((handle.process_id, handle.window_id), (0, 0))

    def test_observation_requires_content_proof_and_valid_bounded_png(self):
        cases = (
            {"session_state": "active", "png_bytes": b"not png", "content_verified": True,
             "sampled_color_count": 4, "accessibility": {}},
            {"session_state": "active", "png_bytes": PNG, "content_verified": False,
             "sampled_color_count": 4, "accessibility": {}},
            {"session_state": "active", "png_bytes": PNG, "content_verified": True,
             "sampled_color_count": 2, "accessibility": {}},
            {"session_state": "active", "png_bytes": PNG, "content_verified": True,
             "sampled_color_count": 4, "accessibility": {"large": "x" * 17000}},
        )
        for observation in cases:
            with self.subTest(keys=tuple(observation)):
                backend, _ = self.make_backend(self.make_session(observation=observation))
                handle = self.start(backend)
                with self.assertRaises(BackendOperationError):
                    backend.observe(handle, timeout_seconds=5)
                backend.stop(handle, timeout_seconds=5)

    def test_private_capture_diagnostic_code_is_preserved_on_backend_error(self):
        backend, _ = self.make_backend(self.make_session(observation={
            "error_code": "capture_print_window_failed"}))
        handle = self.start(backend)
        with self.assertRaises(BackendOperationError) as caught:
            backend.observe(handle, timeout_seconds=5)
        self.assertEqual(caught.exception.interrupt_reason, "capture_failed")
        self.assertEqual(caught.exception.diagnostic_code,
                         "capture_print_window_failed")
        backend.stop(handle, timeout_seconds=5)

    def test_action_is_allowlisted_and_postcondition_must_match_exactly(self):
        backend, _ = self.make_backend(self.make_session())
        handle = self.start(backend)
        with self.assertRaisesRegex(BackendOperationError, "ACTION_NOT_ALLOWLISTED"):
            backend.act(handle, "other_action", timeout_seconds=5)
        wrong = {"performed": True, "postcondition": {"panel_state": "closed"},
                 "target_absent_from_input_desktop": True,
                 "input_desktop_unchanged": True}
        bad_backend, _ = self.make_backend(self.make_session(action=wrong))
        bad_handle = self.start(bad_backend)
        with self.assertRaisesRegex(BackendOperationError,
                                   "TARGET_ACTION_POSTCONDITION_FAILED"):
            bad_backend.act(bad_handle, "open_panel", timeout_seconds=5)
        backend.stop(handle, timeout_seconds=5)
        bad_backend.stop(bad_handle, timeout_seconds=5)

    def test_stop_does_not_claim_success_when_any_cleanup_predicate_is_false(self):
        session = self.make_session(stop={
            "stopped": True, "target_stopped": True, "job_closed": True,
            "desktop_removed": False, "input_desktop_unchanged": True,
        })
        backend, _ = self.make_backend(session)
        receipt = backend.stop(self.start(backend), timeout_seconds=5)
        self.assertFalse(receipt.verified)

    def test_action_descriptor_is_passed_as_manager_owned_immutable_policy(self):
        session = self.make_session()
        backend, _ = self.make_backend(session)
        handle = self.start(backend)
        backend.act(handle, "open_panel", timeout_seconds=5)
        self.assertIs(session.invoked[0], self.action)

    def test_task_dispatcher_full_lifecycle_with_fake_output_lease(self):
        session = self.make_session()
        backend, _ = self.make_backend(session)

        class FakeOutputLease:
            def __init__(self):
                self.acquired = []
                self.heartbeats = 0
                self.releases = []

            def acquire(self, lease_id, request):
                self.acquired.append((lease_id, request.as_dict()))
                return {"eligible": True, "endpoint_keys": ["test-output-a", "test-output-b"]}

            def heartbeat(self, lease_id):
                self.heartbeats += 1
                return True

            def release(self, lease_id):
                self.releases.append(lease_id)
                return True

        from uuid import uuid4
        from core.runtime_packet import PROTOCOL_VERSION

        lease = FakeOutputLease()
        runtime = TaskSessionRuntime(
            backend=backend, target_registry={self.target.app_id: self.target},
            output_lease=lease, watchdog_interval=0)
        runtime.init()
        try:
            started = runtime.handle_request({
                "protocol_version": 2, "request_id": str(uuid4()), "action": "start",
                "target_app_id": self.target.app_id, "confirmed": True,
                "output_request": self.output_request.as_dict(),
            })
            self.assertEqual(started["state"], "ready")
            session_id = started["session_id"]
            observed = runtime.handle_request({
                "protocol_version": 2, "request_id": str(uuid4()), "action": "observe",
                "session_id": session_id,
            })
            self.assertEqual(observed["state"], "running")
            header, png = decode_runtime_packet(observed["packet"])
            self.assertEqual(header["protocol_version"], PROTOCOL_VERSION)
            self.assertEqual(png, PNG)
            acted = runtime.handle_request({
                "protocol_version": 2, "request_id": str(uuid4()), "action": "act",
                "session_id": session_id, "action_id": "open_panel",
            })
            self.assertEqual(acted["state"], "running")
            self.assertTrue(acted["action_result"]["status_verified"])
            stopped = runtime.handle_request({
                "protocol_version": 2, "request_id": str(uuid4()), "action": "stop",
                "session_id": session_id,
            })
            self.assertEqual(stopped["state"], "stopped")
            self.assertTrue(all(stopped["stop_receipt"].values()))
            self.assertEqual(len(lease.acquired), 1)
            self.assertGreaterEqual(lease.heartbeats, 2)
            self.assertEqual(lease.releases, [session_id])
        finally:
            runtime.dispose()


if __name__ == "__main__":
    unittest.main()
