from __future__ import annotations

import base64
import json
import os
import time
import uuid
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.only_chat_output_lease.module import (
    ALLOWED_SCOPE, CONSUMER_ID, INSTANCE_ID, PIPE_PATH, OnlyChatOutputLeaseModule,
)
from modules.task_display_lease.replay_store import ReplayNonceStore


PID = 7341
RUN = "c9071396-0607-44b4-b8b1-922f00523f60"
CLIENT = "a8d47479-df8b-496e-9f8d-f484d340b4c4"
LEASE = "ab5cf196-70ec-414b-b510-cce2de0d54d8"
ENDPOINT_KEY = "\\\\.\\DISPLAY10\nDISPLAY\\MTT1337\\0000\n\\\\?\\DISPLAY#MTT1337#0000#{GUID}"


def nonce(index):
    return base64.urlsafe_b64encode(bytes([index]) * 32).decode("ascii").rstrip("=")


class FakeServer:
    def __init__(self, handler, **kwargs):
        self.handler = handler
        self.options = kwargs
        self.ready = False
        self.active_clients = 0

    def start(self):
        self.ready = True

    def stop(self):
        self.ready = False


class FakePool:
    def __init__(self):
        self.heartbeats = []
        self.releases = []
        self.eligible = True

    def task_leaseable_endpoint_keys(self, _modes):
        if not self.eligible:
            return {"eligible": False, "endpoint_keys": []}
        return {"eligible": True, "endpoint_keys": [ENDPOINT_KEY]}

    def heartbeat(self, lease_id, *, confirmed=False):
        if confirmed is not True:
            raise AssertionError("manager authorization missing")
        self.heartbeats.append(lease_id)

    def release(self, lease_id):
        self.releases.append(lease_id)


def topology():
    return {"topology_verified": True, "topology_generation": "gen-7",
            "mtt_device_instance_id": "ROOT\\DISPLAY\\0000",
            "displays": [{
                "display_name": "\\\\.\\DISPLAY10",
                "monitor_device_id": "DISPLAY\\MTT1337\\0000",
                "monitor_path": "\\\\?\\DISPLAY#MTT1337#0000#{GUID}",
                "monitor_parent": "ROOT\\DISPLAY\\0000", "attached": True,
                "virtual": True, "monitor_identity_verified": True,
                "topology_verified": True, "verified": True, "cloned": False,
                "clone_source_count": 1, "width": 2560, "height": 1440,
                "refresh_rate": 60,
            }]}


class OnlyChatOutputLeaseModuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.exe = self.root / "Only Chat.exe"
        self.exe.write_bytes(b"synthetic Only Chat image fixture")
        self.registry = self.root / "registry.json"
        self.registry.write_text(json.dumps({"apps": [{
            "id": CONSUMER_ID, "name": "Only Chat",
            "windows": {"installed": str(self.root), "installedVerified": True},
        }]}), encoding="utf-8")
        self.server = None
        self.pool = FakePool()
        self.topology = topology()

        def factory(handler, **kwargs):
            self.server = FakeServer(handler, **kwargs)
            return self.server

        self.module = OnlyChatOutputLeaseModule(
            registry_path=self.registry, server_factory=factory,
            process_image_checker=lambda pid, path: pid == PID and path == self.exe,
            id_factory=lambda: LEASE,
            monotonic=lambda: 100.0,
            utc_now=lambda: "2026-10-30T14:00:00Z",
            replay_store_factory=lambda: ReplayNonceStore(self.root / "replay.json"))

    def tearDown(self):
        if self.module._server is not None:
            self.module.dispose()
        self.temp.cleanup()

    def request(self, action, *, rid="b103fb87-3030-43e2-9f77-ece8e46c02d5",
                nonce_value=None, scope=None, **extra):
        request = {"version": 2, "request_id": rid, "nonce": nonce_value or nonce(1),
                   "action": action, "consumer_id": CONSUMER_ID, "run_id": RUN,
                   "workbench_pid": PID, "client_session_id": CLIENT,
                   "instance_id": INSTANCE_ID, "lease_id": LEASE, **extra}
        if action == "acquire":
            request.pop("lease_id")
            request["requested_scope"] = scope if scope is not None else ["display-output"]
            request["requested_ttl_seconds"] = 120
        return request

    def bind(self):
        self.module.init()
        self.module.bind_dependencies(pool=self.pool, topology_reader=lambda: self.topology)
        self.module.start_listener()

    def test_protocol_is_separate_and_grants_only_display_output_scope(self):
        self.bind()
        self.assertTrue(self.server.ready)
        self.assertEqual(self.server.options["pipe_path"], PIPE_PATH)
        self.assertEqual(PIPE_PATH, r"\\.\pipe\OnlyChatAiVirtualOutputLease.v2")
        self.assertEqual(self.server.options["max_request_bytes"], 8192)
        self.assertTrue(self.server.options["client_authenticator"](PID))
        self.assertFalse(self.server.options["client_authenticator"](PID + 1))
        reply = self.module._handle_authenticated(self.request("acquire"), PID)
        self.assertEqual(reply["state"], "acquired")
        self.assertEqual(reply["display"]["device_path"], topology()["displays"][0]["monitor_path"])
        self.assertEqual(reply["display_scope"], ["display-output"])
        self.assertEqual(self.pool.heartbeats, [LEASE])
        self.assertEqual(self.module.refresh()["active_lease_count"], 1)
        stopped = self.module._handle_authenticated(self.request(
            "release", rid="49d92d8b-9896-4ff8-a7f4-590f3bb9b203", nonce_value=nonce(3)), PID)
        self.assertEqual(stopped["state"], "released")
        self.assertEqual(self.pool.releases, [LEASE])

    def test_output_lease_rejects_app_execution_capture_and_input_scopes(self):
        self.bind()
        scopes = (["launch-client"], ["client-input"], ["framebuffer-capture"],
                  ["display-output", "framebuffer-capture"], [{"scope": "display-output"}])
        for index, scope in enumerate(scopes, 10):
            reply = self.module._handle_authenticated(self.request(
                "acquire", rid=f"a0000000-0000-4000-8000-{index:012x}",
                nonce_value=nonce(index), scope=scope), PID)
            self.assertEqual(reply["error_code"], "SCOPE_INVALID")
        self.assertEqual(self.module.refresh()["active_lease_count"], 0)

    def test_topology_or_manager_pool_mismatch_fails_closed(self):
        self.bind()
        for index, mutate in enumerate((lambda row: row.__setitem__("cloned", True),
                                        lambda row: row.__setitem__("monitor_parent", "ROOT\\DISPLAY\\9999")), 20):
            row = self.topology["displays"][0]
            mutate(row)
            reply = self.module._handle_authenticated(self.request(
                "acquire", rid=f"a0000000-0000-4000-8000-{index:012x}",
                nonce_value=nonce(index)), PID)
            self.assertEqual(reply["error_code"], "NO_ACTIVE_UNMIRRORED_OUTPUT")
            self.topology = topology()
        self.pool.eligible = False
        reply = self.module._handle_authenticated(self.request(
            "acquire", rid="a0000000-0000-4000-8000-000000000022", nonce_value=nonce(22)), PID)
        self.assertEqual(reply["error_code"], "NO_ACTIVE_UNMIRRORED_OUTPUT")
        self.assertEqual(self.pool.heartbeats, [])

    def test_only_chat_identity_must_resolve_to_exact_registry_image(self):
        self.assertTrue(self.module._peer_authorized(PID))
        data = json.loads(self.registry.read_text(encoding="utf-8"))
        data["apps"][0]["windows"]["installed"] = str(self.root / "missing")
        self.registry.write_text(json.dumps(data), encoding="utf-8")
        self.assertFalse(self.module._peer_authorized(PID))

    def test_status_and_hidden_self_check_remain_fail_closed(self):
        status = self.module.refresh()
        self.assertFalse(status["pipe_ready"])
        self.assertFalse(status["provider_available"])
        self.assertEqual(status["lease_acquisition"], "fail_closed")
        with patch.object(sys, "argv", ["manager.exe", "--self-check"]):
            self.module.init()
            try:
                self.assertFalse(self.module.refresh()["pipe_ready"])
                self.assertEqual(self.module.refresh()["error_code"], "BROKER_NOT_STARTED_IN_SELF_CHECK")
            finally:
                self.module.dispose()

    def test_registered_module_loads_without_binding_a_process_global_pipe(self):
        from core.module_host import ModuleHost
        host = ModuleHost(ROOT)
        try:
            info = host.load("only_chat_output_lease")
            self.assertEqual(info["version"], "0.1.0")
            self.assertFalse(host.refresh("only_chat_output_lease")["pipe_ready"])
        finally:
            host.dispose_all()

    @unittest.skipUnless(sys.platform == "win32", "requires Windows named pipes")
    def test_windows_unique_pipe_delivers_one_bounded_output_lease(self):
        import pywintypes
        import win32file

        current_pid = os.getpid()
        module = OnlyChatOutputLeaseModule(
            registry_path=self.registry,
            process_image_checker=lambda pid, path: pid == current_pid and path == self.exe,
            id_factory=lambda: LEASE,
            monotonic=lambda: 100.0,
            utc_now=lambda: "2026-10-30T14:00:00Z",
            replay_store_factory=lambda: ReplayNonceStore(self.root / "wire-replay.json"))
        unique_path = rf"\\.\pipe\OnlyChatAiLeaseQA_{uuid.uuid4().hex}"
        request = self.request("acquire", rid="a0000000-0000-4000-8000-000000000030",
                               nonce_value=nonce(30))
        request["workbench_pid"] = current_pid
        from modules.only_chat_output_lease import module as lease_module
        module.init()
        module.bind_dependencies(pool=self.pool, topology_reader=lambda: self.topology)
        handle = None
        with patch.object(lease_module, "PIPE_PATH", unique_path):
            try:
                self.assertTrue(module.start_listener())
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
                            raise TimeoutError("Unique Only Chat output lease pipe did not become ready") from exc
                        time.sleep(0.01)
                win32file.WriteFile(handle, json.dumps(request, separators=(",", ":")).encode() + b"\n")
                _, raw = win32file.ReadFile(handle, 8192)
                response = json.loads(raw.rstrip(b"\n").decode("utf-8"))
                self.assertEqual(response["state"], "acquired")
                self.assertEqual(response["display_scope"], ["display-output"])
                self.assertEqual(response["display"]["device_path"], self.topology["displays"][0]["monitor_path"])
                self.assertEqual(self.pool.heartbeats, [LEASE])
            finally:
                if handle is not None:
                    handle.Close()
                if module._authority and module._authority._leases:
                    module._authority.handle({
                        "version": 2, "action": "release",
                        "request_id": "a0000000-0000-4000-8000-000000000031",
                        "nonce": nonce(31), "consumer_id": CONSUMER_ID,
                        "run_id": RUN, "workbench_pid": current_pid,
                        "client_session_id": CLIENT, "instance_id": INSTANCE_ID,
                        "lease_id": LEASE,
                    }, current_pid)
                module.dispose()


if __name__ == "__main__":
    unittest.main(verbosity=2)
