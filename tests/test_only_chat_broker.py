from __future__ import annotations

import sys
import time
import unittest
import json
import uuid
from unittest.mock import Mock, patch

from app import only_chat_broker_summary
from modules.only_chat_debug_display.module import (
    HOST_INPUT_DESKTOP_INTERRUPTED, HOST_INPUT_DESKTOP_UNKNOWN, NamedPipeServer,
    IDLE_TIMEOUT_SECONDS, OnlyChatDebugDisplayModule, PIPE_PATH, read_input_desktop_state,
)


REQUEST_ID = "2a62b388-3339-4e2c-8ff3-57314cb971ee"
RUN_ID = "9053ad6d-604c-4f7f-8f3b-0b6058a60bfe"
EXPECTED_FIELDS = {
    "version", "request_id", "state", "run_id", "idle_seconds",
    "idle_timeout_seconds", "display_enabled", "error_code",
}


class FakeServer:
    def __init__(self, _handler):
        self.ready = False
        self.active_clients = 0
        self.start = Mock(side_effect=self._start)
        self.stop = Mock(side_effect=self._stop)

    def _start(self):
        self.ready = True

    def _stop(self):
        self.ready = False


class BrokerProtocolTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer(None)
        self.desktop_state = "default"
        self.module = OnlyChatDebugDisplayModule(
            lambda _handler: self.server,
            input_desktop_reader=lambda: self.desktop_state,
        )

    def tearDown(self):
        if self.module._initialized:
            self.module.dispose()

    def request(self, action, **extra):
        return {"version": 1, "request_id": REQUEST_ID, "action": action, **extra}

    def assert_contract_reply(self, reply, *, run_id=None, error_code):
        self.assertEqual(set(reply), EXPECTED_FIELDS)
        self.assertEqual(reply["version"], 1)
        self.assertEqual(reply["request_id"], REQUEST_ID)
        self.assertEqual(reply["state"], "failed")
        self.assertEqual(reply["run_id"], run_id)
        self.assertEqual(reply["idle_seconds"], 0)
        self.assertEqual(reply["idle_timeout_seconds"], IDLE_TIMEOUT_SECONDS)
        self.assertIs(reply["display_enabled"], False)
        self.assertEqual(reply["error_code"], error_code)

    def test_pipe_identity_and_status_reply_match_only_chat_contract(self):
        self.assertEqual(PIPE_PATH, r"\\.\pipe\OnlyChatAiDebugDisplay.v1")
        self.module.init()
        reply = self.module._handle_request(self.request("status"))
        self.assert_contract_reply(reply, error_code="ISOLATED_DESKTOP_UNAVAILABLE")
        state = self.module.refresh()
        self.assertTrue(state["connected"])
        self.assertEqual(state["lease_acquisition"], "fail_closed")

    @unittest.skipUnless(sys.platform == "win32", "requires the Windows named-pipe client identity API")
    def test_authenticated_dispatch_passes_os_peer_pid_only_after_identity_check(self):
        seen = []
        server = NamedPipeServer(
            lambda _request: {"route": "legacy"},
            client_authenticator=lambda pid: pid == 8124,
            rejected_handler=lambda _request: {"state": "rejected"},
            authenticated_handler=lambda request, pid: seen.append((request, pid)) or {"pid": pid},
        )
        request = {"action": "acquire"}
        self.assertEqual(server._dispatch_request(request, 8124), {"pid": 8124})
        self.assertEqual(seen, [(request, 8124)])
        self.assertEqual(server._dispatch_request(request, 9999), {"state": "rejected"})
        self.assertEqual(len(seen), 1)

    @unittest.skipUnless(sys.platform == "win32", "requires Windows named pipes")
    def test_real_named_pipe_status_round_trip_uses_a_unique_disposable_endpoint(self):
        import win32file
        import pywintypes

        pipe_path = rf"\\.\pipe\AIVDD_QA_{uuid.uuid4().hex}"
        cases = (
            (self.request("status"), None, "ISOLATED_DESKTOP_UNAVAILABLE"),
            (self.request("acquire", run_id=RUN_ID, confirmed=True), None,
             "ISOLATED_DESKTOP_UNAVAILABLE"),
            (self.request("heartbeat", run_id=RUN_ID), RUN_ID, "NO_ACTIVE_LEASE"),
            (self.request("release", run_id=RUN_ID), RUN_ID, "NO_ACTIVE_LEASE"),
        )
        module = OnlyChatDebugDisplayModule(input_desktop_reader=lambda: "default")
        with patch("modules.only_chat_debug_display.module.PIPE_PATH", pipe_path):
            module.init()
            try:
                self.assertTrue(module.refresh()["connected"])
                for request, expected_run_id, expected_error in cases:
                    frame = json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n"
                    deadline = time.monotonic() + 3.0
                    while True:
                        try:
                            handle = win32file.CreateFile(
                                pipe_path,
                                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                                0,
                                None,
                                win32file.OPEN_EXISTING,
                                0,
                                None,
                            )
                            break
                        except pywintypes.error as exc:
                            if not exc.args or exc.args[0] not in (2, 231):
                                raise
                            if time.monotonic() >= deadline:
                                raise TimeoutError("Disposable named-pipe instance did not return") from exc
                            time.sleep(0.01)
                    try:
                        win32file.WriteFile(handle, frame)
                        _, response_bytes = win32file.ReadFile(handle, 4097)
                    finally:
                        handle.Close()
                    response = json.loads(response_bytes.rstrip(b"\n").decode("utf-8"))
                    self.assert_contract_reply(response, run_id=expected_run_id,
                                               error_code=expected_error)
            finally:
                module.dispose()

    def test_acquire_is_always_rejected_without_private_desktop_backend(self):
        reply = self.module._handle_request(self.request(
            "acquire", run_id=RUN_ID, confirmed=True))
        self.assert_contract_reply(reply, error_code="ISOLATED_DESKTOP_UNAVAILABLE")

    def test_acquire_requires_explicit_boolean_confirmation(self):
        for value in (False, 1, "true", None):
            with self.subTest(value=value):
                reply = self.module._handle_request(self.request(
                    "acquire", run_id=RUN_ID, confirmed=value))
                self.assert_contract_reply(reply, error_code="CONFIRMATION_REQUIRED")

    def test_non_default_input_desktop_surfaces_interrupt_and_still_fails_closed(self):
        self.desktop_state = "other"
        for action, extra in (("status", {}), ("acquire", {"run_id": RUN_ID, "confirmed": True})):
            with self.subTest(action=action):
                reply = self.module._handle_request(self.request(action, **extra))
                self.assert_contract_reply(reply, error_code=HOST_INPUT_DESKTOP_INTERRUPTED)
                self.assertIs(reply["display_enabled"], False)

    def test_unknown_input_desktop_fails_closed_with_specific_error(self):
        self.desktop_state = "unknown"
        reply = self.module._handle_request(self.request("status"))
        self.assert_contract_reply(reply, error_code=HOST_INPUT_DESKTOP_UNKNOWN)

    def test_desktop_reader_exception_is_unknown_and_no_prompt_action_is_exposed(self):
        module = OnlyChatDebugDisplayModule(
            server_factory=lambda _handler: FakeServer(None),
            input_desktop_reader=Mock(side_effect=OSError("denied")),
        )
        module.init()
        try:
            reply = module._handle_request(self.request("status"))
            self.assert_contract_reply(reply, error_code=HOST_INPUT_DESKTOP_UNKNOWN)
            self.assertFalse(any(hasattr(module, name) for name in ("click", "approve", "dismiss")))
        finally:
            module.dispose()

    def test_live_input_desktop_probe_returns_a_closed_classification(self):
        state = read_input_desktop_state()
        self.assertIn(state, {"default", "other", "unknown"})

    def test_unmatched_heartbeat_and_release_never_create_or_release_a_lease(self):
        for action in ("heartbeat", "release"):
            with self.subTest(action=action):
                reply = self.module._handle_request(self.request(action, run_id=RUN_ID))
                self.assert_contract_reply(reply, run_id=RUN_ID, error_code="NO_ACTIVE_LEASE")

    def test_bad_version_unknown_action_extra_field_and_bad_run_id_fail_closed(self):
        cases = [
            ({"version": True, "request_id": REQUEST_ID, "action": "status"}, "REQUEST_INVALID"),
            (self.request("start"), "REQUEST_INVALID"),
            ({**self.request("status"), "extra": 1}, "REQUEST_INVALID"),
            (self.request("acquire", run_id="not-a-uuid", confirmed=True), "RUN_ID_INVALID"),
        ]
        for request, code in cases:
            with self.subTest(request=request):
                reply = self.module._handle_request(request)
                self.assertEqual(reply["error_code"], code)
                self.assertIs(reply["display_enabled"], False)

    def test_snapshot_blocks_active_request_and_pipe_listener_reload(self):
        self.module.init()
        self.server.active_clients = 1
        with self.assertRaisesRegex(RuntimeError, "request is active"):
            self.module.snapshot()
        self.server.active_clients = 0
        with self.assertRaisesRegex(RuntimeError, "manager restart"):
            self.module.snapshot()

    def test_dispose_stops_only_its_owned_pipe_server(self):
        self.module.init()
        self.module.dispose()
        self.server.stop.assert_called_once_with()
        self.assertFalse(self.module.refresh()["connected"])

    def test_startup_failure_keeps_manager_module_loadable_and_reports_reason(self):
        broken = FakeServer(None)
        broken.start = Mock(side_effect=OSError("PIPE_BUSY"))
        broken.stop = Mock()
        module = OnlyChatDebugDisplayModule(lambda _handler: broken,
                                            input_desktop_reader=lambda: "default")
        module.init()
        self.assertEqual(module.refresh()["error_code"], "OSError: PIPE_BUSY")
        broken.stop.assert_called_once_with()
        module.dispose()

    def test_self_check_does_not_bind_the_process_global_pipe(self):
        factory = Mock(side_effect=AssertionError("self-check must not bind the live global pipe"))
        module = OnlyChatDebugDisplayModule(factory,
                                            input_desktop_reader=lambda: "default")
        with patch.object(sys, "argv", ["manager.exe", "--self-check"]):
            module.init()
            self.assertFalse(module.refresh()["connected"])
            self.assertEqual(module.refresh()["error_code"], "BROKER_NOT_STARTED_IN_SELF_CHECK")
        factory.assert_not_called()
        module.dispose()

    def test_manager_copy_is_truthful_about_connection_and_missing_isolation(self):
        connected = only_chat_broker_summary({"connected": True, "input_desktop_state": "default"})
        self.assertIn("프로토콜 연결됨", connected)
        self.assertIn("대여는 거부", connected)
        unavailable = only_chat_broker_summary({"connected": False,
                                                "input_desktop_state": "default",
                                                "error_code": "PIPE_BUSY"})
        self.assertIn("PIPE_BUSY", unavailable)
        self.assertIn("격리 실행은 사용할 수 없습니다", unavailable)
        interrupted = only_chat_broker_summary({"connected": True, "input_desktop_state": "other"})
        self.assertIn("작업을 중지", interrupted)
        self.assertIn("자동으로 누르지 않습니다", interrupted)


if __name__ == "__main__":
    unittest.main()
