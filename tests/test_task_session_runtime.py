from __future__ import annotations

import hashlib
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.png_capture import encode_bgra_png
from core.runtime_packet import decode_runtime_packet
from core.module_host import ModuleHost
from app import bind_task_runtime_dependencies
from modules.task_session_runtime.module import (
    BackendOperationError, BackendStartError, Observation, OutputRequest, StopReceipt, TargetActionDescriptor,
    TargetHandle, TargetLaunchDescriptor, TargetSpec, TaskOutputLeaseCoordinator, TaskSessionRuntime,
    verify_target_executable_identity,
)


TARGET = TargetSpec("blender", frozenset({"toggle_xray", "select_cube"}))
PIXELS = bytes((0, 0, 0, 0, 0, 0, 255, 0,
                0, 255, 0, 0, 255, 0, 0, 0))
PNG = encode_bgra_png(PIXELS, 2, 2)


def request(action, **fields):
    if action == "start":
        fields.setdefault("output_request", {"output_count": 2, "profile": "high"})
    return {"protocol_version": 2, "request_id": str(uuid.uuid4()),
            "action": action, **fields}


class FakeBackend:
    def __init__(self, *, proof=True, clock=None, block_act=False, block_start=False):
        self.proof = proof
        self.clock = clock
        self.block_act = block_act
        self.block_start = block_start
        self.start_entered = threading.Event()
        self.start_continue = threading.Event()
        self.observe_entered = threading.Event()
        self.observe_continue = threading.Event()
        self.act_entered = threading.Event()
        self.act_continue = threading.Event()
        self.started = []
        self.acted = []
        self.stopped = []

    def start(self, spec, session_id, *, output_request, timeout_seconds):
        self.started.append((spec.app_id, session_id, timeout_seconds, output_request.as_dict()))
        self.start_entered.set()
        if self.block_start and not self.start_continue.wait(2):
            raise TimeoutError("test start gate timed out")
        return TargetHandle(
            opaque=session_id, backend="fake_private_desktop", process_id=2001,
            window_id=3001, job_owned=True,
            window_absent_from_input_desktop=self.proof,
            input_desktop_unchanged=True,
        )

    def observe(self, handle, *, timeout_seconds):
        self.observe_entered.set()
        if getattr(self, "block_observe", False) and not self.observe_continue.wait(2):
            raise TimeoutError("test observation gate timed out")
        if getattr(self, "observe_error", None) is not None:
            raise self.observe_error
        return Observation(PNG, {"elements": [{"role": "button", "name": "X-Ray"}]})

    def act(self, handle, action_id, *, timeout_seconds):
        self.act_entered.set()
        if self.block_act:
            if not self.act_continue.wait(2):
                raise TimeoutError("test action gate timed out")
        self.acted.append(action_id)
        return {"action_id": action_id, "performed": True}

    def stop(self, handle, *, timeout_seconds):
        self.stopped.append(handle.opaque)
        return StopReceipt(True, True, True, True, True)


class TargetLaunchDescriptorTests(unittest.TestCase):
    def make_descriptor(self, executable, actions=None):
        data = executable.read_bytes()
        return TargetLaunchDescriptor(
            executable_path=str(executable.resolve()),
            executable_sha256=hashlib.sha256(data).hexdigest(),
            arguments=("--safe-start",),
            working_directory=str(executable.parent.resolve()),
            window_class_names=("AllowedTargetWindow",),
            actions=tuple(actions or (TargetActionDescriptor(
                "open_panel", "Button", "Open panel", "Panel", "Open"),)),
        )

    def test_target_executable_hash_is_checked_and_detects_file_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "owned-target.exe"
            executable.write_bytes(b"synthetic executable identity")
            descriptor = self.make_descriptor(executable)
            self.assertEqual(verify_target_executable_identity(descriptor), {
                "verified": True, "error_code": None,
                "sha256": descriptor.executable_sha256,
            })
            executable.write_bytes(b"replaced image")
            self.assertEqual(verify_target_executable_identity(descriptor), {
                "verified": False, "error_code": "TARGET_IMAGE_HASH_MISMATCH",
            })


    def test_launch_descriptor_rejects_relative_paths_mutable_argv_and_bad_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "owned-target.exe"
            executable.write_bytes(b"test")
            valid = self.make_descriptor(executable)
            for changes in (
                {"executable_path": "relative.exe"},
                {"working_directory": "relative"},
                {"arguments": ["--safe-start"]},
                {"executable_sha256": "A" * 64},
                {"window_class_names": ()},
                {"capture_policy": "host_screen_fallback"},
                {"maximum_session_seconds": 86401},
            ):
                with self.subTest(changes=changes):
                    values = dict(valid.__dict__)
                    values.update(changes)
                    with self.assertRaises(ValueError):
                        TargetLaunchDescriptor(**values)

    def test_launch_descriptor_allows_only_bounded_trailing_class_prefix(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "owned-target.exe"
            executable.write_bytes(b"test")
            valid = self.make_descriptor(executable)
            router_prefix = "WindowsForms10.Window.8.app.0.*"
            values = dict(valid.__dict__)
            values["window_class_names"] = (router_prefix,)
            self.assertEqual(TargetLaunchDescriptor(**values).window_class_names,
                             (router_prefix,))
            for invalid in ("*", "TooShort*", "WindowsForms10.*.Window",
                            "WindowsForms10.Window.8.app.0.**"):
                with self.subTest(pattern=invalid):
                    bad = dict(valid.__dict__)
                    bad["window_class_names"] = (invalid,)
                    with self.assertRaises(ValueError):
                        TargetLaunchDescriptor(**bad)

    def test_target_registry_requires_action_ids_to_match_immutable_ui_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "owned-target.exe"
            executable.write_bytes(b"test")
            action = TargetActionDescriptor(
                "open_panel", "Button", "Open panel", "Panel", "Open")
            descriptor = self.make_descriptor(executable, (action,))
            good = TargetSpec("demo.target", frozenset({"open_panel"}), descriptor)
            runtime = TaskSessionRuntime(target_registry={good.app_id: good}, watchdog_interval=0)
            self.assertEqual(runtime.refresh()["target_count"], 1)
            with self.assertRaisesRegex(ValueError, "Invalid manager-owned target registry"):
                TaskSessionRuntime(target_registry={
                    good.app_id: TargetSpec(good.app_id, frozenset({"different_action"}), descriptor)
                }, watchdog_interval=0)

    def test_action_descriptor_requires_exact_bounded_control_and_postcondition(self):
        for values in (
            {"action_id": "Bad ID"},
            {"control_type": "Text"},
            {"control_name": ""},
            {"postcondition_value": "x" * 257},
        ):
            args = {"action_id": "open_panel", "control_type": "Button",
                    "control_name": "Open panel", "postcondition_name": "Panel",
                    "postcondition_value": "Open"}
            args.update(values)
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    TargetActionDescriptor(**args)

    def test_target_output_policy_rejects_invalid_bounds_and_mutable_profiles(self):
        for changes in (
            {"minimum_output_count": 1},
            {"maximum_output_count": 9},
            {"minimum_output_count": 4, "maximum_output_count": 3},
            {"output_profiles": {"high"}},
            {"output_profiles": frozenset({"custom"})},
            {"output_profiles": frozenset()},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                TargetSpec("demo.target", frozenset(), **changes)


class RuntimeCompositionTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "requires Windows named pipes")
    def test_composition_uses_hash_loaded_types_and_binds_same_runtime_to_broker(self):
        host = ModuleHost(ROOT)
        try:
            host.load("task_session_runtime")
            host.load("only_chat_task_runtime")
            loaded = host._loaded["task_session_runtime"]
            task_broker = host._loaded["only_chat_task_runtime"].instance
            host._loaded["only_chat_task_runtime"].module.PIPE_PATH = (
                rf"\\.\pipe\AIVDD_COMPOSE_QA_{uuid.uuid4().hex}")
            # This is the production failure signature: the package import and
            # ModuleHost's hash-qualified load hold distinct class identities.
            from modules.task_session_runtime.module import TaskOutputLeaseCoordinator
            self.assertIsNot(
                TaskOutputLeaseCoordinator,
                loaded.module.TaskOutputLeaseCoordinator,
            )
            pool = type("Pool", (), {"task_lease_policy": lambda *_args: {}})()
            coordinator = bind_task_runtime_dependencies(
                loaded, pool, lambda: {}, task_broker)
            self.assertIsInstance(coordinator, loaded.module.TaskOutputLeaseCoordinator)
            self.assertIs(loaded.instance._output_lease, coordinator)
            self.assertIs(task_broker._runtime, loaded.instance)
            state = task_broker.refresh()
            self.assertTrue(state["runtime_bound"])
            self.assertTrue(state["pipe_ready"])
            self.assertFalse(state["backend_available"])
            self.assertEqual(state["error_code"], "ISOLATED_BACKEND_UNAVAILABLE")
        finally:
            host.dispose_all()


class TaskOutputLeaseCoordinatorTests(unittest.TestCase):
    class PoolBackend:
        def __init__(self):
            from modules.virtual_output_pool.module import mode_identity_key
            self.count = 1
            self.modes = {mode_identity_key("DISPLAY1", "MONITOR1", "PATH1"):
                          (800, 600, 60)}

        def write_count(self, count):
            from modules.virtual_output_pool.module import mode_identity_key
            self.count = count
            self.modes = {mode_identity_key(f"DISPLAY{i}", f"MONITOR{i}", f"PATH{i}"):
                          self.modes.get(mode_identity_key(f"DISPLAY{i}", f"MONITOR{i}",
                                                           f"PATH{i}"), (800, 600, 60))
                          for i in range(1, count + 1)}
            return True

        def write_mode(self, mode):
            self.modes = {key: tuple(mode) for key in self.modes}
            return True

    def make_pool(self):
        from modules.virtual_output_pool.module import OutputPoolModule, PROFILES
        backend = self.PoolBackend()
        pool = OutputPoolModule(require_empty_baseline=False)
        pool.configure_backend(count_reader=lambda: backend.count,
            count_writer=backend.write_count, mode_reader=lambda: dict(backend.modes),
            mode_writer=backend.write_mode,
            mode_restorer=lambda previous: (backend.modes.update(
                {key: tuple(value) for key, value in previous.items()}) or True),
            output_window_count=lambda: 0)
        pool.begin(3, "high", set(PROFILES.values()), confirmed=True)
        parent = r"ROOT\DISPLAY\0000"
        topology = {"topology_verified": True,
                    "mtt_device_instance_id": parent, "displays": []}
        for i in range(1, 4):
            topology["displays"].append({
                "display_name": f"DISPLAY{i}", "monitor_device_id": f"MONITOR{i}",
                "monitor_path": f"PATH{i}", "monitor_parent": parent,
                "virtual": True, "attached": True, "monitor_identity_verified": True,
                "topology_verified": True, "verified": True, "cloned": False,
                "clone_source_count": 1, "width": 2560, "height": 1440,
                "refresh_rate": 60, "x": i * 2560, "y": 0,
            })
        return pool, backend, topology

    def make_target(self, executable):
        data = executable.read_bytes()
        action = TargetActionDescriptor("toggle_xray", "Button", "X-Ray",
                                        "X-Ray enabled", "true")
        launch = TargetLaunchDescriptor(
            executable_path=str(executable.resolve()),
            executable_sha256=hashlib.sha256(data).hexdigest(), arguments=(),
            working_directory=str(executable.parent.resolve()),
            window_class_names=("BlenderWindow",), actions=(action,))
        return TargetSpec("blender", frozenset({action.action_id}), launch)

    def test_target_start_leases_only_new_outputs_and_verified_stop_releases_them(self):
        from modules.virtual_output_pool.module import mode_identity_key
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "target.exe"
            executable.write_bytes(b"synthetic executable")
            target = self.make_target(executable)
            pool, pool_backend, topology = self.make_pool()
            coordinator = TaskOutputLeaseCoordinator(pool, lambda: topology)
            backend = FakeBackend()
            runtime = TaskSessionRuntime(backend=backend, target_registry={"blender": target},
                                         output_lease=coordinator, watchdog_interval=0)
            runtime.init()
            try:
                started = runtime.handle_request(request(
                    "start", target_app_id="blender", confirmed=True,
                    output_request={"output_count": 2, "profile": "high"}))
                self.assertEqual(started["state"], "ready")
                self.assertEqual(started["output_endpoint_keys"], [
                    mode_identity_key("DISPLAY2", "MONITOR2", "PATH2"),
                    mode_identity_key("DISPLAY3", "MONITOR3", "PATH3")])
                self.assertEqual(pool.status()["leases"], 1)
                self.assertEqual(len(backend.started), 1)
                stopped = runtime.handle_request(request("stop", session_id=started["session_id"]))
                self.assertEqual(stopped["state"], "stopped")
                self.assertTrue(stopped["stop_receipt"]["output_lease_released"])
                self.assertEqual(pool.status()["leases"], 0)
                self.assertEqual(pool_backend.count, 3)
            finally:
                runtime.dispose()

    def test_empty_private_desktop_preflight_failure_releases_task_and_pool_lease(self):
        from modules.private_desktop.session import DiagnosticSessionStartFailure
        from modules.task_session_runtime.isolated_target_backend import (
            DescriptorPrivateDesktopBackend,
        )

        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "owned-target.exe"
            executable.write_bytes(b"synthetic executable for preflight lease regression")
            target = self.make_target(executable)
            pool, _pool_backend, topology = self.make_pool()
            coordinator = TaskOutputLeaseCoordinator(pool, lambda: topology)

            class ExitedOwner:
                ident = 7654

                @staticmethod
                def is_alive():
                    return False

            class EmptyStartup:
                _thread = ExitedOwner()
                _state = {"stop_receipt": {
                    "desktop_created": False, "job_created": False,
                    "process_created": False, "window_created": False,
                    "input_desktop_unchanged": False,
                }}

            failed_session = EmptyStartup()
            failure = DiagnosticSessionStartFailure(
                "host input desktop identity is unknown", session=failed_session,
                cleanup_receipt=failed_session._state["stop_receipt"],
                error_code="HOST_INPUT_DESKTOP_UNKNOWN")
            backend = DescriptorPrivateDesktopBackend(
                lambda *_args, **_kwargs: (_ for _ in ()).throw(failure))
            runtime = TaskSessionRuntime(
                backend=backend, target_registry={target.app_id: target},
                output_lease=coordinator, watchdog_interval=0)
            runtime.init()
            try:
                reply = runtime.handle_request(request(
                    "start", target_app_id=target.app_id, confirmed=True,
                    output_request={"output_count": 2, "profile": "high"}))
                self.assertEqual(reply["state"], "failed")
                self.assertEqual(reply["error_code"], "HOST_INPUT_DESKTOP_UNKNOWN")
                self.assertIsNone(runtime.refresh()["active_session_id"])
                self.assertEqual(pool.status()["leases"], 0)
                countdown = pool.reconcile()
                self.assertEqual(countdown["state"], "idle_countdown")
            finally:
                runtime.dispose()

    def test_readiness_requires_a_live_matching_pool_and_exact_verified_endpoints(self):
        pool, _backend, topology = self.make_pool()
        coordinator = TaskOutputLeaseCoordinator(pool, lambda: topology)
        target = self.make_target(Path(sys.executable).resolve())
        ready = coordinator.readiness(target)
        self.assertEqual(ready, {"eligible": True, "output_count": 2, "profile": "high"})

        original_policy = pool.task_lease_policy
        first_key = "DISPLAY2\nMONITOR2\nPATH2"
        pool.task_lease_policy = lambda *_args: {
            "eligible": True, "endpoint_keys": [first_key, first_key]}
        duplicate = coordinator.readiness(target)
        self.assertEqual(duplicate, {"eligible": False,
                                     "error_code": "OUTPUT_ENDPOINT_SET_INVALID"})
        acquired = coordinator.acquire("duplicate-output-test",
                                       OutputRequest(2, "high"))
        self.assertEqual(acquired, {"eligible": False,
                                    "error_code": "OUTPUT_ENDPOINT_SET_INVALID"})
        self.assertEqual(pool.status()["leases"], 0)
        pool.task_lease_policy = original_policy

        mismatched = TargetSpec("blender", target.action_ids, launch=target.launch,
                                minimum_output_count=3, maximum_output_count=4,
                                output_profiles=frozenset({"high"}))
        rejected = coordinator.readiness(mismatched)
        self.assertEqual(rejected, {"eligible": False, "error_code": "OUTPUT_COUNT_MISMATCH"})

        pool.reconcile(force=True, confirmed=True)
        off = coordinator.readiness(target)
        self.assertEqual(off, {"eligible": False, "error_code": "OUTPUT_POOL_NOT_VERIFIED"})

    def test_cancel_during_output_acquisition_prevents_target_launch(self):
        from modules.virtual_output_pool.module import mode_identity_key
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "target.exe"
            executable.write_bytes(b"synthetic executable")
            target = self.make_target(executable)
            pool, _pool_backend, topology = self.make_pool()
            original_heartbeat = pool.heartbeat
            lease_acquired = threading.Event()
            finish_acquire = threading.Event()

            def pause_after_pool_commit(lease_id, *, confirmed=False):
                result = original_heartbeat(lease_id, confirmed=confirmed)
                lease_acquired.set()
                if not finish_acquire.wait(2):
                    raise TimeoutError("test lease acquisition gate timed out")
                return result

            pool.heartbeat = pause_after_pool_commit
            original_release = pool.release
            release_attempts = [0]

            def fail_first_release(lease_id):
                release_attempts[0] += 1
                if release_attempts[0] == 1:
                    return {"released": False, "error_code": "INJECTED_ONCE"}
                return original_release(lease_id)

            pool.release = fail_first_release
            coordinator = TaskOutputLeaseCoordinator(pool, lambda: topology)
            backend = FakeBackend()
            runtime = TaskSessionRuntime(backend=backend, target_registry={"blender": target},
                                         output_lease=coordinator, watchdog_interval=0)
            runtime.init()
            start_result, cancel_result = [], []
            start_request = request("start", target_app_id="blender", confirmed=True,
                                    output_request={"output_count": 2, "profile": "high"})
            start_thread = threading.Thread(
                target=lambda: start_result.append(runtime.handle_request(start_request)))
            cancel_thread = None
            try:
                start_thread.start()
                self.assertTrue(lease_acquired.wait(1), "lease acquire did not reach gate")
                session_id = runtime.refresh()["active_session_id"]
                self.assertIsNotNone(session_id)
                cancel_request = request("stop", session_id=session_id)
                cancel_thread = threading.Thread(
                    target=lambda: cancel_result.append(runtime.handle_request(cancel_request)))
                cancel_thread.start()
                deadline = __import__("time").monotonic() + 1
                while __import__("time").monotonic() < deadline:
                    with runtime._lock:
                        session = runtime._active
                        if session is not None and session.cancel_requested:
                            break
                    threading.Event().wait(0.005)
                with runtime._lock:
                    self.assertTrue(runtime._active.cancel_requested)
                finish_acquire.set()
                start_thread.join(2)
                cancel_thread.join(2)
                self.assertFalse(start_thread.is_alive())
                self.assertFalse(cancel_thread.is_alive())
                self.assertEqual(len(start_result), 1)
                self.assertEqual(len(cancel_result), 1)
                self.assertEqual(start_result[0]["state"], "failed")
                self.assertEqual(start_result[0]["error_code"], "CLEANUP_UNVERIFIED")
                self.assertEqual(cancel_result[0]["state"], "stopped")
                self.assertEqual(backend.started, [])
                self.assertEqual(pool.status()["leases"], 0)
                self.assertIsNone(runtime.refresh()["active_session_id"])
                self.assertFalse(start_result[0]["stop_receipt"]["output_lease_released"])
                self.assertEqual(release_attempts[0], 2)
                self.assertEqual(start_result[0]["output_endpoint_keys"], [
                    mode_identity_key("DISPLAY2", "MONITOR2", "PATH2"),
                    mode_identity_key("DISPLAY3", "MONITOR3", "PATH3")])
            finally:
                finish_acquire.set()
                start_thread.join(2)
                if cancel_thread is not None:
                    cancel_thread.join(2)
                runtime.dispose()

    def test_topology_change_interrupts_target_and_verified_cleanup_releases_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "target.exe"
            executable.write_bytes(b"synthetic executable")
            target = self.make_target(executable)
            pool, _pool_backend, topology = self.make_pool()
            coordinator = TaskOutputLeaseCoordinator(pool, lambda: topology)
            backend = FakeBackend()
            runtime = TaskSessionRuntime(backend=backend, target_registry={"blender": target},
                                         output_lease=coordinator, watchdog_interval=0)
            runtime.init()
            try:
                started = runtime.handle_request(request(
                    "start", target_app_id="blender", confirmed=True,
                    output_request={"output_count": 2, "profile": "high"}))
                self.assertEqual(started["state"], "ready")
                topology["displays"][2]["cloned"] = True
                interrupted = runtime.handle_request(
                    request("observe", session_id=started["session_id"]))
                self.assertEqual(interrupted["state"], "stopped")
                self.assertEqual(interrupted["error_code"], "OUTPUT_LEASE_LOST")
                self.assertEqual(backend.stopped, [started["session_id"]])
                self.assertEqual(pool.status()["leases"], 0)
            finally:
                runtime.dispose()

    def test_clone_or_profile_mismatch_never_creates_a_pool_lease(self):
        pool, _backend, topology = self.make_pool()
        coordinator = TaskOutputLeaseCoordinator(pool, lambda: topology)
        topology["displays"][2]["cloned"] = True
        lease_id = str(uuid.uuid4())
        self.assertEqual(coordinator.acquire(lease_id, OutputRequest(2, "high"))["error_code"],
                         "OUTPUT_ENDPOINT_NOT_ISOLATED")
        self.assertEqual(pool.status()["leases"], 0)
        topology["displays"][2]["cloned"] = False
        self.assertEqual(coordinator.acquire(str(uuid.uuid4()), OutputRequest(2, "ultra"))[
            "error_code"], "OUTPUT_PROFILE_MISMATCH")
        self.assertEqual(pool.status()["leases"], 0)

    def test_launchable_target_fails_closed_without_output_coordinator(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "target.exe"
            executable.write_bytes(b"synthetic executable")
            target = self.make_target(executable)
            backend = FakeBackend()
            runtime = TaskSessionRuntime(backend=backend, target_registry={"blender": target},
                                         watchdog_interval=0)
            runtime.init()
            try:
                reply = runtime.handle_request(request(
                    "start", target_app_id="blender", confirmed=True))
                self.assertEqual(reply["error_code"], "OUTPUT_LEASE_COORDINATOR_UNAVAILABLE")
                self.assertEqual(backend.started, [])
            finally:
                runtime.dispose()


class TaskSessionRuntimeTests(unittest.TestCase):
    def make_runtime(self, backend=None, targets=None, clock=None):
        runtime = TaskSessionRuntime(backend=backend, target_registry=targets or {},
                                     clock=clock or __import__("time").monotonic,
                                     watchdog_interval=0)
        runtime.init()
        return runtime

    def start(self, runtime, target="blender"):
        return runtime.handle_request(request("start", target_app_id=target, confirmed=True))

    def test_start_routes_only_user_confirmed_output_request_within_target_policy(self):
        backend = FakeBackend()
        target = TargetSpec("blender", frozenset({"toggle_xray"}),
                            minimum_output_count=2, maximum_output_count=3,
                            output_profiles=frozenset({"high"}))
        runtime = self.make_runtime(backend, {"blender": target})
        try:
            reply = runtime.handle_request(request(
                "start", target_app_id="blender", confirmed=True,
                output_request={"output_count": 3, "profile": "high"}))
            self.assertEqual(reply["state"], "ready")
            self.assertEqual(reply["output_request"], {"output_count": 3, "profile": "high"})
            self.assertIn("allowlisted_actions", reply["capabilities"])
            self.assertEqual(backend.started[0][3], {"output_count": 3, "profile": "high"})
        finally:
            runtime.dispose()

    def test_observe_only_target_does_not_advertise_or_dispatch_actions(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "observe-only.exe"
            executable.write_bytes(b"synthetic observe-only target")
            launch = TargetLaunchDescriptor(
                executable_path=str(executable.resolve()),
                executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
                arguments=("--isolated-qa",), working_directory=str(executable.parent.resolve()),
                window_class_names=("ObserveOnlyWindow",), actions=())
            target = TargetSpec(
                "observe_only", frozenset(), launch=launch,
                minimum_output_count=0, maximum_output_count=0,
                output_profiles=frozenset({"private"}), isolation_mode="private_desktop")
            backend = FakeBackend()
            runtime = self.make_runtime(backend, {target.app_id: target})
            try:
                started = runtime.handle_request(request(
                    "start", target_app_id=target.app_id, confirmed=True,
                    output_request={"output_count": 0, "profile": "private"}))
                self.assertEqual(started["state"], "ready")
                self.assertEqual(started["capabilities"], ["observe", "interrupt", "stop"])
                refused = runtime.handle_request(request(
                    "act", session_id=started["session_id"], action_id="anything"))
                self.assertEqual(refused["error_code"], "ACTION_NOT_ALLOWLISTED")
                self.assertEqual(backend.acted, [])
            finally:
                runtime.dispose()

    def test_observe_reply_preserves_bounded_capture_diagnostic_code(self):
        backend = FakeBackend()
        backend.observe_error = BackendOperationError(
            "TARGET_OBSERVE_FAILED", interrupt_reason="capture_failed",
            diagnostic_code="capture_print_window_failed")
        runtime = self.make_runtime(backend, {"blender": TARGET})
        try:
            started = self.start(runtime)
            observed = runtime.handle_request(request(
                "observe", session_id=started["session_id"]))
            self.assertEqual(observed["state"], "interrupted")
            self.assertEqual(observed["error_code"], "capture_failed")
            self.assertEqual(observed["diagnostic_code"],
                             "capture_print_window_failed")
        finally:
            runtime.dispose()

    def test_observe_reply_drops_unrecognized_diagnostic_code(self):
        backend = FakeBackend()
        backend.observe_error = BackendOperationError(
            "TARGET_OBSERVE_FAILED", interrupt_reason="capture_failed",
            diagnostic_code="capture_private_path_C_sensitive")
        runtime = self.make_runtime(backend, {"blender": TARGET})
        try:
            started = self.start(runtime)
            observed = runtime.handle_request(request(
                "observe", session_id=started["session_id"]))
            self.assertEqual(observed["error_code"], "capture_failed")
            self.assertNotIn("diagnostic_code", observed)
        finally:
            runtime.dispose()

    def test_start_rejects_malformed_or_policy_exceeding_output_requests(self):
        backend = FakeBackend()
        target = TargetSpec("blender", frozenset({"toggle_xray"}),
                            minimum_output_count=2, maximum_output_count=3,
                            output_profiles=frozenset({"high"}))
        runtime = self.make_runtime(backend, {"blender": target})
        invalid = (
            (None, "OUTPUT_REQUEST_INVALID"),
            ({}, "OUTPUT_REQUEST_INVALID"),
            ({"output_count": True, "profile": "high"}, "OUTPUT_REQUEST_INVALID"),
            ({"output_count": 1, "profile": "high"}, "OUTPUT_REQUEST_INVALID"),
            ({"output_count": 9, "profile": "high"}, "OUTPUT_REQUEST_INVALID"),
            ({"output_count": 2.5, "profile": "high"}, "OUTPUT_REQUEST_INVALID"),
            ({"output_count": 2, "profile": "unknown"}, "OUTPUT_REQUEST_INVALID"),
            ({"output_count": 2, "profile": "high", "path": "C:\\bad.exe"}, "OUTPUT_REQUEST_INVALID"),
            ({"output_count": 2, "profile": "balanced"}, "OUTPUT_REQUEST_OUTSIDE_TARGET_POLICY"),
            ({"output_count": 4, "profile": "high"}, "OUTPUT_REQUEST_OUTSIDE_TARGET_POLICY"),
        )
        try:
            for value, expected in invalid:
                with self.subTest(value=value):
                    reply = runtime.handle_request(request(
                        "start", target_app_id="blender", confirmed=True,
                        output_request=value))
                    self.assertEqual(reply["error_code"], expected)
            self.assertEqual(backend.started, [])
        finally:
            runtime.dispose()

    def test_status_exposes_allowlisted_output_requirements_without_target_paths(self):
        target = TargetSpec("blender", frozenset({"toggle_xray"}),
                            minimum_output_count=2, maximum_output_count=4,
                            output_profiles=frozenset({"high", "ultra"}))
        runtime = self.make_runtime(None, {"blender": target})
        try:
            reply = runtime.handle_request(request("status"))
            self.assertEqual(reply["target_policies"], [{
                "target_app_id": "blender", "minimum_output_count": 2,
                "maximum_output_count": 4, "output_profiles": ["high", "ultra"],
                "output_lease_required": False, "output_lease_ready": True,
                "output_lease_error": None, "available_output_count": None,
                "available_output_profile": None,
            }])
            self.assertNotIn("executable_path", str(reply))
        finally:
            runtime.dispose()

    def test_status_fails_when_isolated_launch_target_has_no_output_lease(self):
        executable = Path(sys.executable).resolve()
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        action = TargetActionDescriptor("open_panel", "Button", "Open panel",
                                        "Panel", "Open")
        launch = TargetLaunchDescriptor(
            executable_path=str(executable), executable_sha256=digest,
            arguments=(), working_directory=str(executable.parent),
            window_class_names=("AllowedTargetWindow",), actions=(action,))
        target = TargetSpec("blender", frozenset({"open_panel"}), launch=launch)
        runtime = self.make_runtime(FakeBackend(), {"blender": target})
        try:
            reply = runtime.handle_request(request("status"))
            self.assertEqual(reply["state"], "failed")
            self.assertEqual(reply["error_code"],
                             "OUTPUT_LEASE_COORDINATOR_UNAVAILABLE")
            self.assertTrue(reply["output_lease_required"])
            self.assertFalse(reply["output_lease_bound"])
            pool = type("Pool", (), {"task_lease_policy": lambda *_args: {}})()
            runtime.bind_output_lease(TaskOutputLeaseCoordinator(pool, lambda: []))
            ready = runtime.handle_request(request("status"))
            self.assertEqual(ready["state"], "failed")
            self.assertEqual(ready["error_code"], "OUTPUT_POOL_NOT_READY")
            self.assertTrue(ready["output_lease_bound"])
            self.assertFalse(ready["output_lease_ready"])
            pool, _backend, topology = TaskOutputLeaseCoordinatorTests().make_pool()
            runtime.bind_output_lease(TaskOutputLeaseCoordinator(pool, lambda: topology))
            ready = runtime.handle_request(request("status"))
            self.assertEqual(ready["state"], "running")
            self.assertIsNone(ready["error_code"])
            self.assertTrue(ready["output_lease_ready"])
            pool.reconcile(force=True, confirmed=True)
        finally:
            runtime.dispose()

    def test_watchdog_interval_rejects_nonfinite_negative_and_unbounded_values(self):
        invalid_values = (True, -0.001, float("nan"), float("inf"),
                          float("-inf"), 60.001, "1", 10 ** 10000)
        for index, value in enumerate(invalid_values):
            with self.subTest(index=index, type=type(value).__name__):
                with self.assertRaises(ValueError):
                    TaskSessionRuntime(watchdog_interval=value)

    def test_watchdog_interval_accepts_zero_and_sixty_second_boundary(self):
        for value in (0, 60):
            with self.subTest(value=value):
                runtime = TaskSessionRuntime(watchdog_interval=value)
                runtime.init()
                runtime.dispose()

    def test_default_runtime_is_fail_closed_and_has_no_targets(self):
        runtime = self.make_runtime()
        try:
            self.assertIsNone(runtime._watchdog_thread)
            status = runtime.handle_request(request("status"))
            self.assertEqual(status["error_code"], "ISOLATED_BACKEND_UNAVAILABLE")
            self.assertEqual(status["target_count"], 0)
            self.assertEqual(status["lease_acquisition"], "fail_closed")
            denied = self.start(runtime)
            self.assertEqual(denied["error_code"], "TARGET_APP_NOT_ALLOWLISTED")
            self.assertIsNone(runtime.refresh()["active_session_id"])
        finally:
            runtime.dispose()

    def test_arbitrary_path_extra_keys_confirmation_and_request_replay_are_rejected(self):
        backend = FakeBackend()
        runtime = self.make_runtime(backend, {"blender": TARGET})
        try:
            bad = request("start", target_app_id="blender", confirmed=True,
                          executable_path=r"C:\Windows\System32\cmd.exe")
            self.assertEqual(runtime.handle_request(bad)["error_code"], "REQUEST_INVALID")
            unconfirmed = runtime.handle_request(request(
                "start", target_app_id="blender", confirmed=1))
            self.assertEqual(unconfirmed["error_code"], "CONFIRMATION_REQUIRED")
            valid = request("start", target_app_id="blender", confirmed=True)
            self.assertEqual(runtime.handle_request(valid)["state"], "ready")
            self.assertEqual(runtime.handle_request(valid)["error_code"], "REQUEST_REPLAYED")
            self.assertEqual(len(backend.started), 1)
        finally:
            runtime.dispose()

    def test_start_replay_stays_rejected_after_nonmutating_request_cache_churn(self):
        backend = FakeBackend()
        runtime = self.make_runtime(backend, {"blender": TARGET})
        original_start = request("start", target_app_id="blender", confirmed=True)
        try:
            started = runtime.handle_request(original_start)
            self.assertEqual(started["state"], "ready")
            for _ in range(1030):
                status = runtime.handle_request(request("status"))
                self.assertEqual(status["state"], "running")
            replay = runtime.handle_request(original_start)
            self.assertEqual(replay["error_code"], "REQUEST_REPLAYED")
            self.assertEqual(len(backend.started), 1)
        finally:
            runtime.dispose()

    def test_stateful_request_cap_fails_closed_but_still_allows_stop(self):
        backend = FakeBackend()
        runtime = self.make_runtime(backend, {"blender": TARGET})
        try:
            started = self.start(runtime)
            session_id = started["session_id"]
            with patch("modules.task_session_runtime.module.MAX_STATEFUL_REQUESTS", 5):
                for _ in range(4):
                    reply = runtime.handle_request(request(
                        "act", session_id=session_id, action_id="toggle_xray"))
                    self.assertEqual(reply["state"], "running")
                denied = runtime.handle_request(request(
                    "act", session_id=session_id, action_id="toggle_xray"))
                self.assertEqual(denied["error_code"], "REQUEST_LIMIT_REACHED")
                self.assertEqual(len(backend.acted), 4)
            stopped = runtime.handle_request(request("stop", session_id=session_id))
            self.assertEqual(stopped["state"], "stopped")
        finally:
            runtime.dispose()

    def test_stateful_request_replay_ledger_survives_module_snapshot_restore(self):
        first_backend = FakeBackend()
        first = self.make_runtime(first_backend, {"blender": TARGET})
        start_request = request("start", target_app_id="blender", confirmed=True)
        try:
            started = first.handle_request(start_request)
            self.assertEqual(started["state"], "ready")
            stopped = first.handle_request(request("stop", session_id=started["session_id"]))
            self.assertEqual(stopped["state"], "stopped")
            state = first.snapshot()
        finally:
            first.dispose()

        second_backend = FakeBackend()
        second = TaskSessionRuntime(backend=second_backend, target_registry={"blender": TARGET},
                                    watchdog_interval=0)
        try:
            second.restore(state)
            second.init()
            replay = second.handle_request(start_request)
            self.assertEqual(replay["error_code"], "REQUEST_REPLAYED")
            self.assertEqual(second_backend.started, [])
        finally:
            second.dispose()

    def test_restore_rejects_malformed_or_oversized_stateful_request_ledger(self):
        runtime = TaskSessionRuntime()
        runtime.init()
        try:
            with self.assertRaises(ValueError):
                runtime.restore({"state": "idle", "stateful_request_ids": ["bad"]})
            duplicate = str(uuid.uuid4())
            with self.assertRaises(ValueError):
                runtime.restore({"state": "idle", "stateful_request_ids": [duplicate, duplicate]})
            with patch("modules.task_session_runtime.module.MAX_STATEFUL_REQUESTS", 2):
                with self.assertRaises(ValueError):
                    runtime.restore({"state": "idle", "stateful_request_ids": [
                        str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())]})
        finally:
            runtime.dispose()

    def test_unhashable_action_and_interrupt_reason_fail_closed(self):
        runtime = self.make_runtime(FakeBackend(), {"blender": TARGET})
        try:
            invalid_action = {"protocol_version": 2, "request_id": str(uuid.uuid4()),
                              "action": [], "session_id": "x"}
            self.assertEqual(runtime.handle_request(invalid_action)["error_code"],
                             "REQUEST_INVALID")
            started = self.start(runtime)
            bad_interrupt = runtime.handle_request(request(
                "interrupt", session_id=started["session_id"], reason=[]))
            self.assertEqual(bad_interrupt["error_code"], "INTERRUPT_REASON_INVALID")
        finally:
            runtime.dispose()

    def test_start_requires_isolation_proof_and_cleans_unverified_handle(self):
        backend = FakeBackend(proof=False)
        runtime = self.make_runtime(backend, {"blender": TARGET})
        try:
            reply = self.start(runtime)
            self.assertEqual(reply["error_code"], "TARGET_ISOLATION_UNVERIFIED")
            self.assertEqual(backend.stopped, [reply["session_id"]])
            self.assertIsNone(runtime.refresh()["active_session_id"])
        finally:
            runtime.dispose()

    def test_unverified_isolation_handle_is_retained_if_cleanup_fails(self):
        class FlakyCleanupBackend(FakeBackend):
            def __init__(self):
                super().__init__(proof=False)
                self.stop_attempts = 0

            def stop(self, handle, *, timeout_seconds):
                self.stop_attempts += 1
                self.stopped.append(handle.opaque)
                if self.stop_attempts == 1:
                    return StopReceipt(True, False, False, False, True)
                return StopReceipt(True, True, True, True, True)

        backend = FlakyCleanupBackend()
        runtime = self.make_runtime(backend, {"blender": TARGET})
        try:
            failed = self.start(runtime)
            self.assertEqual(failed["error_code"], "CLEANUP_UNVERIFIED")
            self.assertEqual(runtime.refresh()["active_session_id"], failed["session_id"])
            self.assertEqual(runtime._active.handle.opaque, failed["session_id"])
            self.assertEqual(self.start(runtime)["error_code"], "SESSION_ALREADY_ACTIVE")

            cleaned = runtime.handle_request(request("stop", session_id=failed["session_id"]))
            self.assertEqual(cleaned["state"], "stopped")
            self.assertEqual(backend.stop_attempts, 2)
            self.assertIsNone(runtime.refresh()["active_session_id"])
        finally:
            runtime.dispose()

    def test_start_failure_clears_session_only_when_backend_proves_no_target(self):
        class NoTargetBackend(FakeBackend):
            def start(self, spec, session_id, *, output_request, timeout_seconds):
                raise BackendStartError(target_created=False)

        backend = NoTargetBackend()
        runtime = self.make_runtime(backend, {"blender": TARGET})
        try:
            reply = self.start(runtime)
            self.assertEqual(reply["state"], "failed")
            self.assertEqual(reply["error_code"], "TARGET_START_FAILED")
            self.assertIsNone(runtime.refresh()["active_session_id"])
        finally:
            runtime.dispose()

    def test_partial_start_failure_retains_handle_and_requires_verified_cleanup(self):
        class PartialStartBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.stop_attempts = 0
                self.partial_handle = TargetHandle(
                    opaque="partial-target", backend="fake_private_desktop",
                    process_id=2002, window_id=3002, job_owned=True,
                    window_absent_from_input_desktop=True,
                    input_desktop_unchanged=True,
                )

            def start(self, spec, session_id, *, output_request, timeout_seconds):
                self.started.append((spec.app_id, session_id, timeout_seconds, output_request.as_dict()))
                raise BackendStartError(target_created=True, handle=self.partial_handle)

            def stop(self, handle, *, timeout_seconds):
                self.stop_attempts += 1
                if self.stop_attempts == 1:
                    return StopReceipt(True, False, False, False, True)
                return StopReceipt(True, True, True, True, True)

        backend = PartialStartBackend()
        runtime = self.make_runtime(backend, {"blender": TARGET})
        try:
            failed = self.start(runtime)
            self.assertEqual(failed["error_code"], "START_CLEANUP_UNVERIFIED")
            self.assertEqual(runtime.refresh()["active_session_id"], failed["session_id"])
            retry = self.start(runtime)
            self.assertEqual(retry["error_code"], "SESSION_ALREADY_ACTIVE")
            self.assertEqual(len(backend.started), 1)

            stopped = runtime.handle_request(request("stop", session_id=failed["session_id"]))
            self.assertEqual(stopped["state"], "stopped")
            self.assertEqual(backend.stop_attempts, 2)
            self.assertIsNone(runtime.refresh()["active_session_id"])
        finally:
            runtime.dispose()

    def test_unclassified_start_failure_fails_closed_without_relaunch(self):
        class UnknownStartBackend(FakeBackend):
            def start(self, spec, session_id, *, output_request, timeout_seconds):
                self.started.append((spec.app_id, session_id, timeout_seconds, output_request.as_dict()))
                raise OSError("uncertain partial launch")

        backend = UnknownStartBackend()
        runtime = self.make_runtime(backend, {"blender": TARGET})
        try:
            failed = self.start(runtime)
            self.assertEqual(failed["error_code"], "START_CLEANUP_UNVERIFIED")
            self.assertEqual(runtime.refresh()["active_session_id"], failed["session_id"])
            retry = self.start(runtime)
            self.assertEqual(retry["error_code"], "SESSION_ALREADY_ACTIVE")
            cancel = runtime.handle_request(request("cancel", session_id=failed["session_id"]))
            self.assertEqual(cancel["error_code"], "CLEANUP_UNVERIFIED")
            self.assertEqual(len(backend.started), 1)
            with self.assertRaisesRegex(RuntimeError, "cleanup not verified"):
                runtime.dispose()
        finally:
            runtime._active = None
            runtime.dispose()

    def test_observation_is_bounded_runtime_packet_and_action_is_allowlisted(self):
        backend = FakeBackend()
        runtime = self.make_runtime(backend, {"blender": TARGET})
        try:
            started = self.start(runtime)
            observed = runtime.handle_request(request("observe", session_id=started["session_id"]))
            header, png = decode_runtime_packet(observed["packet"])
            self.assertEqual(observed["state"], "running")
            self.assertEqual(header["target_app_id"], "blender")
            self.assertEqual(png, PNG)
            denied = runtime.handle_request(request("act", session_id=started["session_id"],
                                                    action_id="shell_command"))
            self.assertEqual(denied["error_code"], "ACTION_NOT_ALLOWLISTED")
            allowed = runtime.handle_request(request("act", session_id=started["session_id"],
                                                     action_id="toggle_xray"))
            self.assertTrue(allowed["action_result"]["performed"])
            self.assertEqual(backend.acted, ["toggle_xray"])
        finally:
            runtime.dispose()

    def test_interrupt_pauses_actions_until_cancelled(self):
        backend = FakeBackend()
        runtime = self.make_runtime(backend, {"blender": TARGET})
        try:
            started = self.start(runtime)
            session_id = started["session_id"]
            interrupted = runtime.handle_request(request(
                "interrupt", session_id=session_id, reason="policy_prompt_required"))
            self.assertEqual(interrupted["state"], "interrupted")
            refused = runtime.handle_request(request("act", session_id=session_id,
                                                     action_id="toggle_xray"))
            self.assertEqual(refused["error_code"], "policy_prompt_required")
            cancelled = runtime.handle_request(request("cancel", session_id=session_id))
            self.assertEqual(cancelled["state"], "stopped")
            self.assertTrue(cancelled["stop_receipt"]["desktop_removed"])
        finally:
            runtime.dispose()

    def test_cancel_during_inflight_action_cannot_reactivate_session(self):
        backend = FakeBackend(block_act=True)
        runtime = self.make_runtime(backend, {"blender": TARGET})
        started = self.start(runtime)
        session_id = started["session_id"]
        action_result = {}
        cancel_result = {}
        action_thread = threading.Thread(target=lambda: action_result.update(
            runtime.handle_request(request("act", session_id=session_id,
                                           action_id="toggle_xray"))))
        cancel_thread = threading.Thread(target=lambda: cancel_result.update(
            runtime.handle_request(request("cancel", session_id=session_id))))
        try:
            action_thread.start()
            self.assertTrue(backend.act_entered.wait(2))
            cancel_thread.start()
            deadline = __import__("time").monotonic() + 2
            while (not runtime._active.cancel_requested
                   and __import__("time").monotonic() < deadline):
                __import__("time").sleep(0.001)
            self.assertTrue(runtime._active.cancel_requested)
            backend.act_continue.set()
            action_thread.join(2)
            cancel_thread.join(2)
            self.assertFalse(action_thread.is_alive())
            self.assertFalse(cancel_thread.is_alive())
            self.assertEqual(runtime.refresh()["session_state"], "stopped")
            self.assertEqual(cancel_result["state"], "stopped")
            self.assertEqual(len(backend.stopped), 1)
        finally:
            backend.act_continue.set()
            action_thread.join(2)
            if cancel_thread.ident:
                cancel_thread.join(2)
            runtime.dispose()

    def test_cancel_during_start_stops_handle_before_start_can_report_ready(self):
        backend = FakeBackend(block_start=True)
        runtime = self.make_runtime(backend, {"blender": TARGET})
        start_result = {}
        cancel_result = {}
        start_thread = threading.Thread(target=lambda: start_result.update(
            self.start(runtime)))
        try:
            start_thread.start()
            self.assertTrue(backend.start_entered.wait(2))
            with runtime._lock:
                session_id = runtime._active.session_id
            cancel_thread = threading.Thread(target=lambda: cancel_result.update(
                runtime.handle_request(request("cancel", session_id=session_id))))
            cancel_thread.start()
            deadline = __import__("time").monotonic() + 2
            while (not runtime._active.cancel_requested
                   and __import__("time").monotonic() < deadline):
                __import__("time").sleep(0.001)
            self.assertTrue(runtime._active.cancel_requested)
            backend.start_continue.set()
            start_thread.join(2)
            cancel_thread.join(2)
            self.assertFalse(start_thread.is_alive())
            self.assertFalse(cancel_thread.is_alive())
            self.assertEqual(start_result["state"], "stopped")
            self.assertEqual(cancel_result["state"], "stopped")
            self.assertEqual(len(backend.stopped), 1)
            self.assertIsNone(runtime.refresh()["active_session_id"])
        finally:
            backend.start_continue.set()
            start_thread.join(2)
            runtime.dispose()

    def test_interrupt_during_observe_discards_frame_and_pauses_session(self):
        backend = FakeBackend()
        backend.block_observe = True
        runtime = self.make_runtime(backend, {"blender": TARGET})
        started = self.start(runtime)
        session_id = started["session_id"]
        observed = {}
        worker = threading.Thread(target=lambda: observed.update(runtime.handle_request(
            request("observe", session_id=session_id))))
        try:
            worker.start()
            self.assertTrue(backend.observe_entered.wait(2))
            interrupt = runtime.handle_request(request(
                "interrupt", session_id=session_id, reason="host_input_desktop_changed"))
            self.assertEqual(interrupt["state"], "interrupted")
            backend.observe_continue.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(observed["state"], "interrupted")
            self.assertNotIn("packet", observed)
        finally:
            backend.observe_continue.set()
            worker.join(2)
            runtime.dispose()

    def test_heartbeat_idle_timeout_stops_only_that_session(self):
        now = [100.0]
        backend = FakeBackend()
        runtime = self.make_runtime(backend, {"blender": TARGET}, clock=lambda: now[0])
        try:
            started = self.start(runtime)
            now[0] += 301
            reply = runtime.handle_request(request("heartbeat", session_id=started["session_id"]))
            self.assertEqual(reply["state"], "stopped")
            self.assertEqual(reply["error_code"], "IDLE_TIMEOUT")
            self.assertIsNone(runtime.refresh()["active_session_id"])
        finally:
            runtime.dispose()

    def test_idle_watchdog_stops_session_without_a_late_heartbeat(self):
        now = [100.0]
        backend = FakeBackend()
        runtime = TaskSessionRuntime(backend=backend, target_registry={"blender": TARGET},
                                     clock=lambda: now[0], watchdog_interval=0.002)
        runtime.init()
        try:
            started = self.start(runtime)
            now[0] += 301
            deadline = __import__("time").monotonic() + 2
            while (runtime.refresh()["session_state"] != "stopped"
                   and __import__("time").monotonic() < deadline):
                __import__("time").sleep(0.001)
            self.assertEqual(runtime.refresh()["session_state"], "stopped")
            self.assertEqual(backend.stopped, [started["session_id"]])
        finally:
            runtime.dispose()

    def test_idle_watchdog_serializes_expiry_after_inflight_action_and_stops_once(self):
        now = [100.0]
        backend = FakeBackend(block_act=True)
        runtime = TaskSessionRuntime(backend=backend, target_registry={"blender": TARGET},
                                     clock=lambda: now[0], watchdog_interval=0.002)
        runtime.init()
        started = self.start(runtime)
        result = {}
        worker = threading.Thread(target=lambda: result.update(runtime.handle_request(
            request("act", session_id=started["session_id"], action_id="toggle_xray"))))
        try:
            worker.start()
            self.assertTrue(backend.act_entered.wait(2))
            now[0] += 301
            deadline = __import__("time").monotonic() + 2
            while (not runtime._active.cancel_requested
                   and __import__("time").monotonic() < deadline):
                __import__("time").sleep(0.001)
            self.assertTrue(runtime._active.cancel_requested)
            backend.act_continue.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result.get("state"), "stopped")
            self.assertEqual(runtime.refresh()["session_state"], "stopped")
            self.assertEqual(backend.stopped, [started["session_id"]])
        finally:
            backend.act_continue.set()
            worker.join(2)
            runtime.dispose()

    def test_dispose_joins_an_idle_backend_watchdog(self):
        runtime = TaskSessionRuntime(backend=FakeBackend(), target_registry={"blender": TARGET},
                                     watchdog_interval=60)
        runtime.init()
        watchdog = runtime._watchdog_thread
        self.assertIsNotNone(watchdog)
        runtime.dispose()
        self.assertFalse(watchdog.is_alive())
        self.assertIsNone(runtime._watchdog_thread)

    def test_idle_watchdog_keeps_failed_cleanup_recoverable(self):
        class FlakyStopBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.stop_attempts = 0

            def stop(self, handle, *, timeout_seconds):
                self.stop_attempts += 1
                self.stopped.append(handle.opaque)
                if self.stop_attempts == 1:
                    return StopReceipt(True, False, False, False, True)
                return StopReceipt(True, True, True, True, True)

        now = [100.0]
        backend = FlakyStopBackend()
        runtime = TaskSessionRuntime(backend=backend, target_registry={"blender": TARGET},
                                     clock=lambda: now[0], watchdog_interval=0.002)
        runtime.init()
        try:
            started = self.start(runtime)
            now[0] += 301
            deadline = __import__("time").monotonic() + 2
            while (runtime.refresh()["session_state"] != "failed"
                   and __import__("time").monotonic() < deadline):
                __import__("time").sleep(0.001)
            self.assertEqual(runtime.refresh()["session_state"], "failed")
            self.assertEqual(runtime.refresh()["active_session_id"], started["session_id"])
            self.assertEqual(backend.stop_attempts, 1)
            stopped = runtime.handle_request(request("stop", session_id=started["session_id"]))
            self.assertEqual(stopped["state"], "stopped")
            self.assertEqual(backend.stop_attempts, 2)
        finally:
            runtime.dispose()

    def test_failed_stop_is_not_reported_as_cleaned_and_can_retry(self):
        class FlakyStopBackend(FakeBackend):
            def stop(self, handle, *, timeout_seconds):
                self.stopped.append(handle.opaque)
                if len(self.stopped) == 1:
                    return StopReceipt(True, False, False, False, True)
                return StopReceipt(True, True, True, True, True)

        backend = FlakyStopBackend()
        runtime = self.make_runtime(backend, {"blender": TARGET})
        try:
            started = self.start(runtime)
            first = runtime.handle_request(request("stop", session_id=started["session_id"]))
            self.assertEqual(first["state"], "failed")
            self.assertEqual(first["error_code"], "CLEANUP_UNVERIFIED")
            second = runtime.handle_request(request("stop", session_id=started["session_id"]))
            self.assertEqual(second["state"], "stopped")
            self.assertEqual(len(backend.stopped), 2)
        finally:
            runtime.dispose()

    def test_module_reload_is_blocked_while_session_is_active(self):
        runtime = self.make_runtime(FakeBackend(), {"blender": TARGET})
        try:
            self.start(runtime)
            with self.assertRaisesRegex(RuntimeError, "reload is deferred"):
                runtime.snapshot()
        finally:
            runtime.dispose()

    def test_module_loads_through_the_canonical_versioned_host(self):
        host = ModuleHost(ROOT)
        try:
            info = host.load("task_session_runtime")
            state = host.refresh("task_session_runtime")
            self.assertEqual(info["version"], "0.1.21")
            self.assertFalse(state["backend_available"])
            self.assertEqual(state["target_count"], 0)
            self.assertEqual(state["lease_acquisition"], "fail_closed")
        finally:
            host.dispose_all()


if __name__ == "__main__":
    unittest.main(verbosity=2)


