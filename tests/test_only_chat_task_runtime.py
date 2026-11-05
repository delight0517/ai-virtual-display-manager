from __future__ import annotations

import base64
import json
from pathlib import Path
import sys
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from app import task_runtime_broker_summary
from core.png_capture import encode_bgra_png
from core.module_host import ModuleHost
from core.runtime_packet import encode_runtime_packet
from modules.only_chat_task_runtime.module import (
    APP_PATH_REGISTRY, MAX_REPLY_BYTES, MAX_REQUEST_BYTES, PIPE_PATH,
    OnlyChatTaskRuntimeModule, _normalized_executable_path,
    process_image_matches, registered_only_chat_exe_path,
)
from modules.task_session_runtime.module import TaskSessionRuntime


REQUEST_ID = "2a62b388-3339-4e2c-8ff3-57314cb971ee"
SESSION_ID = "9053ad6d-604c-4f7f-8f3b-0b6058a60bfe"


class FakeServer:
    def __init__(self, handler, **kwargs):
        self.handler = handler
        self.authenticated_handler = kwargs.get("authenticated_handler")
        self.client_authenticator = kwargs.get("client_authenticator")
        self.rejected_handler = kwargs.get("rejected_handler")
        self.ready = False
        self.active_clients = 0
        self.start = Mock(side_effect=self._start)
        self.stop = Mock(side_effect=self._stop)

    def _start(self):
        self.ready = True

    def _stop(self):
        self.ready = False

    def dispatch(self, request, client_pid):
        if not self.client_authenticator or not self.client_authenticator(client_pid):
            return self.rejected_handler(request)
        if self.authenticated_handler:
            return self.authenticated_handler(request, client_pid)
        return self.handler(request)


class OnlyChatTaskRuntimePipeTests(unittest.TestCase):
    def setUp(self):
        self.server = None

        def factory(handler, **kwargs):
            self.server = FakeServer(handler, **kwargs)
            self.server.authenticated_handler = self.module._handle_authenticated_request
            self.server.client_authenticator = self.module._client_identity_matches
            self.server.rejected_handler = lambda request: self.module._failure(
                request, "CLIENT_UNAUTHORIZED")
            return self.server

        self.module = OnlyChatTaskRuntimeModule(factory)
        self.module.init()
        self.runtime = TaskSessionRuntime()
        self.runtime.init()

    def tearDown(self):
        if self.module._initialized:
            self.module.dispose()
        self.runtime.dispose()

    def request(self, action, **fields):
        if action == "start":
            fields.setdefault("output_request", {"output_count": 2, "profile": "high"})
        return {"protocol_version": 2, "request_id": REQUEST_ID,
                "action": action, **fields}

    def test_dedicated_pipe_is_distinct_and_bounded(self):
        self.assertEqual(PIPE_PATH, r"\\.\pipe\OnlyChatAiTaskRuntime.v2")
        self.assertNotEqual(PIPE_PATH, r"\\.\pipe\OnlyChatAiDebugDisplay.v1")
        self.assertEqual(MAX_REQUEST_BYTES, 64 * 1024)
        self.assertEqual(MAX_REPLY_BYTES, 6 * 1024 * 1024)

    def test_only_chat_client_identity_uses_exact_normalized_image_path(self):
        expected = r"C:\Apps\Only Chat.exe"
        self.assertEqual(_normalized_executable_path(expected),
                         _normalized_executable_path(r"c:\apps\ONLY CHAT.exe"))
        self.assertNotEqual(_normalized_executable_path(expected),
                            _normalized_executable_path(r"C:\Apps\Only Chat.exe.copy"))
        self.assertFalse(process_image_matches(0, expected))
        self.assertFalse(process_image_matches(True, expected))
        self.assertFalse(process_image_matches(123, r"Z:\missing\Only Chat.exe"))

    def test_client_identity_path_comes_from_exact_registry_record(self):
        root = Path(__file__).resolve().parents[1]
        local = json.loads(APP_PATH_REGISTRY.read_text(encoding="utf-8"))
        record = next(item for item in local["apps"] if item["id"] == "only-chat-windows")
        expected = Path(record["windows"]["installed"]) / f"{record['name']}.exe"
        self.assertEqual(registered_only_chat_exe_path(), expected)
        self.assertIsNone(registered_only_chat_exe_path(root / "missing-registry.json"))

        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            registry = Path(temp) / "APP_PATH_REGISTRY.json"
            registry.write_text(json.dumps({"apps": [{"id": "only-chat-windows",
                "windows": {"installed": "relative\\Only Chat.exe"}}]}), encoding="utf-8")
            self.assertIsNone(registered_only_chat_exe_path(registry))

            install_dir = Path(temp) / "OnlyChat"
            install_dir.mkdir()
            (install_dir / "Only Chat.exe").write_bytes(b"test executable placeholder")
            registry.write_text(json.dumps({"apps": [{"id": "only-chat-windows",
                "name": "Only Chat", "windows": {"installed": str(install_dir)}}]}),
                encoding="utf-8")
            self.assertEqual(registered_only_chat_exe_path(registry), install_dir / "Only Chat.exe")

    def test_pipe_auth_resolves_latest_registry_path_for_each_client(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "OnlyChat-1.0" / "Only Chat.exe"
            second = root / "OnlyChat-1.1" / "Only Chat.exe"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_bytes(b"old")
            second.write_bytes(b"new")
            registry = root / "APP_PATH_REGISTRY.json"

            def write_registry(install_dir):
                registry.write_text(json.dumps({"apps": [{"id": "only-chat-windows",
                    "name": "Only Chat", "windows": {"installed": str(install_dir)}}]}),
                    encoding="utf-8")

            checked = []
            module = OnlyChatTaskRuntimeModule(
                registry_path=registry,
                process_image_checker=lambda pid, expected: checked.append(
                    (pid, Path(expected))) or True)
            try:
                write_registry(first.parent)
                self.assertTrue(module._client_identity_matches(7001))
                write_registry(second.parent)
                self.assertTrue(module._client_identity_matches(7002))
                self.assertEqual(checked, [(7001, first), (7002, second)])
                registry.unlink()
                self.assertFalse(module._client_identity_matches(7003))
                self.assertEqual(len(checked), 2)
            finally:
                module.dispose()

    @unittest.skipUnless(sys.platform == "win32", "requires Windows process image query")
    def test_non_only_chat_process_cannot_authenticate_as_pipe_client(self):
        import os
        # The test must not depend on a developer's separately installed Only Chat app.
        expected = Path(__file__).resolve()
        self.assertTrue(expected.is_file())
        self.assertFalse(process_image_matches(os.getpid(), expected))

    def test_pipe_auth_configuration_requires_both_callbacks(self):
        from modules.only_chat_debug_display.module import NamedPipeServer
        with self.assertRaisesRegex(ValueError, "requires both"):
            NamedPipeServer(lambda _request: {}, client_authenticator=lambda _pid: True)

    def test_manager_status_text_distinguishes_ipc_from_target_readiness(self):
        self.assertIn("수신 대기 실패", task_runtime_broker_summary({
            "pipe_ready": False, "error_code": "BROKER_UNAVAILABLE"}))
        pending = task_runtime_broker_summary({
            "pipe_ready": True, "backend_available": False,
            "target_count": 0, "error_code": "ISOLATED_BACKEND_UNAVAILABLE"})
        self.assertIn("IPC 수신 대기 중", pending)
        self.assertIn("허용 대상 없음", pending)
        self.assertIn("앱 실행은 거부됩니다", pending)
        self.assertIn("Only Chat 인증 요청 수신 기록 없음", pending)
        observed = task_runtime_broker_summary({
            "pipe_ready": True, "backend_available": False, "target_count": 0,
            "authenticated_request_count": 2,
        })
        self.assertIn("Only Chat 인증 요청 수신 2회", observed)
        ready = task_runtime_broker_summary({
            "pipe_ready": True, "backend_available": True, "target_count": 2,
            "output_lease_required": True, "output_lease_bound": True})
        self.assertIn("허용 대상 2개", ready)
        missing_lease = task_runtime_broker_summary({
            "pipe_ready": True, "backend_available": True, "target_count": 1,
            "output_lease_required": True, "output_lease_bound": False})
        self.assertIn("출력 임대 연결 없음", missing_lease)
        self.assertIn("앱 실행은 거부됩니다", missing_lease)
        missing_pool = task_runtime_broker_summary({
            "pipe_ready": True, "backend_available": True, "target_count": 1,
            "output_lease_required": True, "output_lease_bound": True,
            "error_code": "OUTPUT_POOL_NOT_READY"})
        self.assertIn("가상 출력 풀이 확인되지 않아", missing_pool)
        self.assertIn("AI 화면 만들기", missing_pool)
        self.assertIn("앱 실행을 거부합니다", missing_pool)

    def test_refresh_rejects_launch_target_without_output_lease_coordinator(self):
        class RuntimeState:
            def handle_request(_self, request):
                return {"request_id": request.get("request_id"), "state": "failed"}

            def refresh(_self):
                return {"backend_available": True, "target_count": 1,
                        "output_lease_required": True, "output_lease_bound": False}

        self.module.bind_runtime(RuntimeState())
        state = self.module.refresh()
        self.assertEqual(state["error_code"],
                         "OUTPUT_LEASE_COORDINATOR_UNAVAILABLE")
        self.assertFalse(state["output_lease_bound"])
        self.assertTrue(state["output_lease_required"])

    def test_refresh_propagates_verified_pool_readiness_failure(self):
        class RuntimeState:
            def handle_request(_self, request):
                return {"request_id": request.get("request_id"), "state": "failed"}

            def refresh(_self):
                return {"backend_available": True, "target_count": 1,
                        "output_lease_required": True, "output_lease_bound": True,
                        "output_lease_ready": False,
                        "readiness_error": "OUTPUT_POOL_NOT_READY"}

        self.module.bind_runtime(RuntimeState())
        self.server.ready = True
        state = self.module.refresh()
        self.assertEqual(state["error_code"], "OUTPUT_POOL_NOT_READY")
        self.assertFalse(state["output_lease_ready"])

    def test_unbound_endpoint_stays_unavailable_without_starting_a_listener(self):
        reply = self.module._handle_request(self.request("status"))
        self.assertEqual(reply["error_code"], "RUNTIME_NOT_BOUND")
        self.assertFalse(self.module.refresh()["pipe_ready"])
        self.assertEqual(self.module.refresh()["lease_acquisition"], "fail_closed")

    def test_bound_pipe_routes_runtime_status_but_does_not_fake_a_backend(self):
        self.module.bind_runtime(self.runtime)
        self.assertTrue(self.server.ready)
        reply = self.server.handler(self.request("status"))
        self.assertEqual(reply["state"], "failed")
        self.assertEqual(reply["error_code"], "ISOLATED_BACKEND_UNAVAILABLE")
        status = self.module.refresh()
        self.assertTrue(status["runtime_bound"])
        self.assertTrue(status["pipe_ready"])
        self.assertFalse(status["backend_available"])
        self.assertEqual(status["target_count"], 0)
        self.assertEqual(status["lease_acquisition"], "fail_closed")
        self.assertEqual(status["error_code"], "ISOLATED_BACKEND_UNAVAILABLE")

    def test_only_exact_authenticated_client_is_reported_as_consumer_activity(self):
        self.module._expected_client_path = lambda: Path(r"C:\Apps\Only Chat.exe")
        self.module._process_image_checker = lambda pid, _path: pid == 42
        self.module.bind_runtime(self.runtime)
        rejected = self.server.dispatch(self.request("status"), 41)
        self.assertEqual(rejected["error_code"], "CLIENT_UNAUTHORIZED")
        state = self.module.refresh()
        self.assertEqual(state["authenticated_request_count"], 0)
        self.assertIsNone(state["last_authenticated_request_unix_ms"])

        accepted = self.server.dispatch(self.request("status"), 42)
        self.assertTrue(accepted["client_identity_verified"])
        state = self.module.refresh()
        self.assertEqual(state["authenticated_request_count"], 1)
        self.assertIsInstance(state["last_authenticated_request_unix_ms"], int)

    def test_start_is_routed_and_unknown_target_remains_fail_closed(self):
        self.module.bind_runtime(self.runtime)
        reply = self.server.handler(self.request(
            "start", target_app_id="blender", confirmed=True))
        self.assertEqual(reply["state"], "failed")
        self.assertEqual(reply["error_code"], "TARGET_APP_NOT_ALLOWLISTED")
        self.assertIsNone(self.runtime.refresh()["active_session_id"])

    def test_observation_packet_is_bounded_base64_not_raw_json_bytes(self):
        png = encode_bgra_png(bytes((0, 0, 255, 255)), 1, 1)
        packet = encode_runtime_packet({
            "protocol_version": 1,
            "request_id": REQUEST_ID,
            "session_id": SESSION_ID,
            "action": "observe",
        }, png)

        class PacketRuntime:
            def handle_request(_self, request):
                return {"protocol_version": 2, "request_id": request["request_id"],
                        "session_id": SESSION_ID, "state": "running", "error_code": None,
                        "packet": packet}

            def refresh(_self):
                return {"backend_available": True, "target_count": 1,
                        "active_session_id": SESSION_ID}

        self.module.bind_runtime(PacketRuntime())
        response = self.server.handler(self.request("observe", session_id=SESSION_ID))
        self.assertNotIn("packet", response)
        self.assertEqual(base64.b64decode(response["packet_base64"], validate=True), packet)

        invalid = PacketRuntime()
        invalid.handle_request = lambda request: {"packet": b""}
        other = OnlyChatTaskRuntimeModule(lambda handler: FakeServer(handler))
        other.init()
        other.bind_runtime(invalid)
        try:
            self.assertEqual(other._handle_request(self.request("observe"))["error_code"],
                             "OBSERVATION_PACKET_INVALID")
        finally:
            other.dispose()

    def test_runtime_exceptions_are_redacted(self):
        class BrokenRuntime:
            def handle_request(self, _request):
                raise RuntimeError("C:\\private\\path\\secret")

        self.module.bind_runtime(BrokenRuntime())
        reply = self.server.handler(self.request("status"))
        self.assertEqual(reply["error_code"], "RUNTIME_REQUEST_FAILED")
        self.assertNotIn("private", json.dumps(reply))

    def test_manifest_bound_load_is_idle_and_does_not_bind_a_process_global_pipe(self):
        root = Path(__file__).resolve().parents[1]
        host = ModuleHost(root)
        try:
            info = host.load("only_chat_task_runtime")
            self.assertEqual(info["version"], "0.1.7")
            state = host.refresh("only_chat_task_runtime")
            self.assertFalse(state["pipe_ready"])
            self.assertFalse(state["runtime_bound"])
            self.assertEqual(state["error_code"], "RUNTIME_NOT_BOUND")
        finally:
            host.dispose_all()

    def test_hidden_self_check_refuses_to_bind_global_endpoint(self):
        module = OnlyChatTaskRuntimeModule(lambda handler: FakeServer(handler))
        with patch.object(sys, "argv", ["manager.exe", "--self-check"]):
            module.init()
            try:
                with self.assertRaisesRegex(RuntimeError, "Hidden self-check"):
                    module.bind_runtime(self.runtime)
                self.assertFalse(module.refresh()["pipe_ready"])
                self.assertEqual(module.refresh()["error_code"],
                                 "BROKER_NOT_STARTED_IN_SELF_CHECK")
            finally:
                module.dispose()

    def test_bound_listener_blocks_reload_only_during_active_client_and_disposes_cleanly(self):
        self.module.bind_runtime(self.runtime)
        self.assertFalse(self.module.reload_blocked())
        with self.assertRaisesRegex(RuntimeError, "manager restart"):
            self.module.snapshot()
        self.server.active_clients = 1
        self.assertTrue(self.module.reload_blocked())
        self.server.active_clients = 0
        self.module.dispose()
        self.assertFalse(self.module._initialized)
        self.server.stop.assert_called_once_with()

    @unittest.skipUnless(sys.platform == "win32", "requires Windows named pipes")
    def test_real_runtime_pipe_uses_unique_disposable_endpoint(self):
        import pywintypes
        import win32file

        unique_path = rf"\\.\pipe\AIVDD_TASK_QA_{uuid.uuid4().hex}"
        module = OnlyChatTaskRuntimeModule()
        runtime = TaskSessionRuntime()
        runtime.init()
        with patch("modules.only_chat_task_runtime.module.PIPE_PATH", unique_path):
            module.init()
            module.bind_runtime(runtime)
            try:
                self.assertTrue(module.refresh()["pipe_ready"])
                frame = json.dumps(self.request("status"), separators=(",", ":")).encode() + b"\n"
                deadline = time.monotonic() + 3
                while True:
                    try:
                        handle = win32file.CreateFile(
                            unique_path, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                            0, None, win32file.OPEN_EXISTING, 0, None)
                        break
                    except pywintypes.error as exc:
                        if not exc.args or exc.args[0] not in (2, 231):
                            raise
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Disposable task-session pipe did not become ready") from exc
                        time.sleep(0.01)
                try:
                    win32file.WriteFile(handle, frame)
                    _, raw = win32file.ReadFile(handle, 4097)
                finally:
                    handle.Close()
                reply = json.loads(raw.rstrip(b"\n").decode("utf-8"))
                self.assertEqual(reply["request_id"], REQUEST_ID)
                self.assertEqual(reply["error_code"], "CLIENT_UNAUTHORIZED")
                self.assertEqual(reply["state"], "failed")
            finally:
                module.dispose()
                runtime.dispose()


if __name__ == "__main__":
    unittest.main()

