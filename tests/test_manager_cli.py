from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from modules.manager_cli.module import (
    ManagerCliModule, PROTOCOL_VERSION, _parse_vdd_settings_modes,
)


class FakeServer:
    def __init__(self, handler, **kwargs):
        self.handler = handler
        self.kwargs = kwargs
        self.ready = False
        self.active_clients = 0
        self.stopped = False

    def start(self):
        self.ready = True

    def stop(self):
        self.ready = False
        self.stopped = True


class ManagerCliTests(unittest.TestCase):
    def test_configured_mode_evidence_is_validated_and_labeled(self):
        self.module._configured_mode_reader = lambda: {
            "modes": [[2560, 1440, 60], [1920, 1080, 60], [2560, 1440, 60]],
            "sha256": "a" * 64,
        }
        self.assertEqual(self.module.configured_output_mode_evidence(), {
            "modes": [[1920, 1080, 60], [2560, 1440, 60]],
            "source": "configured_vdd_settings", "sha256": "a" * 64,
        })

        self.module._configured_mode_reader = lambda: {
            "modes": [[2560, True, 60]], "sha256": "a" * 64,
        }
        with self.assertRaisesRegex(ValueError, "OUTPUT_MODE_EVIDENCE_UNAVAILABLE"):
            self.module.configured_output_mode_evidence()

    def test_packaged_peer_is_exact_companion_and_same_user(self):
        from unittest.mock import patch
        observed=[]
        self.module._process_image_checker=lambda pid,path: observed.append((pid,path)) or True
        with patch.object(sys,'frozen',True,create=True), patch.object(sys,'executable',str(ROOT/'candidate'/'AI Virtual Display Manager.exe')):
            server=self.module._new_server()
            self.assertTrue(server.kwargs['client_authenticator'](111))
            self.assertFalse(server.kwargs['client_authenticator'](222))
        self.assertEqual(observed[0][1],ROOT/'candidate'/'vdmctl.exe')

    def setUp(self):
        self.now = 1_000.0
        self.topology = {
            "topology_verified": True,
            "mtt_device_instance_id": "ROOT\\DISPLAY\\0000",
            "displays": [
                {"display_name": "\\\\.\\DISPLAY1", "target_name": "Physical",
                 "width": 2560, "height": 1440, "refresh_rate": 60, "virtual": False,
                 "attached": True, "verified": True, "topology_verified": True,
                 "cloned": False, "mirroring_driver": False, "primary": True,
                 "monitor_identity_verified": True, "monitor_path": "PHYSICAL/PATH",
                 "available_modes": [[2560, 1440, 60]]},
                {"display_name": "\\\\.\\DISPLAY32", "target_name": "VDD by MTT",
                 "width": 1920, "height": 1080, "refresh_rate": 60, "virtual": True,
                 "attached": True, "verified": True, "topology_verified": True,
                 "cloned": False, "mirroring_driver": False, "primary": False,
                 "monitor_identity_verified": True, "monitor_parent": "ROOT\\DISPLAY\\0000",
                 "monitor_path": "MTT/PATH",
                 "available_modes": [[1920, 1080, 60], [2560, 1440, 60]]},
            ],
        }
        self.pool = {"state": "off", "leases": 0}
        self.begin_calls = []
        self.stop_calls = []
        self.task_calls = []
        self.ids = iter(["a" * 32, "b" * 32])
        self.server = None
        self.configured_mode_evidence = {
            "modes": [[2560, 1440, 60], [1920, 1080, 60]],
            "sha256": "f" * 64,
        }
        self.module = ManagerCliModule(
            server_factory=self._server_factory,
            process_image_checker=lambda pid, _path: pid == 111,
            user_process_checker=lambda pid: pid == 111,
            clock=lambda: self.now,
            id_factory=lambda: next(self.ids),
            log_path=Path(tempfile.gettempdir()) / f"vdm-cli-{uuid.uuid4().hex}.jsonl",
            configured_mode_reader=lambda: self.configured_mode_evidence,
        )
        self.module.init()
        self.module.bind_dependencies(
            status_reader=lambda: {"app_version": "test", "topology": self.topology},
            topology_reader=lambda: self.topology,
            pool_status=lambda: self.pool,
            pool_begin=self._pool_begin,
            pool_stop=self._pool_stop,
            task_action=self._task_action,
            window_reader=lambda: {"windows": [{"app": "browser", "pid": 321,
                                                 "bounds": [0, 0, 800, 600],
                                                 "window_state": "normal",
                                                 "display_name": "\\\\.\\DISPLAY1",
                                                 "title": "private title"}],
                                   "virtual_outputs": [{"display_name": "\\\\.\\DISPLAY32",
                                                        "bounds": [0, 0, 1920, 1080],
                                                        "width": 1920, "height": 1080}],
                                   "movement_requires_confirmation": True},
        )

    def tearDown(self):
        self.module.dispose()
        if self.module._log_path and self.module._log_path.exists():
            self.module._log_path.unlink()

    def _server_factory(self, handler, **kwargs):
        self.server = FakeServer(handler, **kwargs)
        return self.server

    def _pool_begin(self, payload):
        self.begin_calls.append(payload)
        return {"state": "active", "plan": {"outputs_to_add": payload["output_count"]}}

    def _pool_stop(self):
        self.stop_calls.append(True)
        return {"state": "off", "verified": True}

    def _task_action(self, action, payload):
        self.task_calls.append((action, payload))
        return {"state": "running" if action == "start" else "stopped",
                "request_id": payload["request_id"], "session_id": "00000000-0000-4000-8000-000000000001"}

    def _request(self, command, arguments, request_id=None):
        return {"protocol_version": PROTOCOL_VERSION,
                "request_id": request_id or str(uuid.uuid4()),
                "issued_at_ms": int(self.now * 1000),
                "command": command, "arguments": arguments}

    def test_pipe_is_local_authenticated_and_bounded(self):
        self.assertTrue(self.server.ready)
        self.assertEqual(self.module._server.kwargs["pipe_path"], r"\\.\pipe\AIVDM_ManagerControl.v1")
        self.assertEqual(self.module._control_server.kwargs["pipe_path"], r"\\.\pipe\AIVDM_TaskControl.v1")
        self.assertEqual(self.server.kwargs["max_request_bytes"], 16_384)
        self.assertTrue(self.server.kwargs["client_authenticator"](111))
        self.assertFalse(self.server.kwargs["client_authenticator"](222))
        self.assertIn("authenticated_handler", self.server.kwargs)

    def test_status_and_logs_are_read_only_and_logs_drop_unapproved_fields(self):
        status = self.module._handle_request(self._request("status", {}))
        self.assertTrue(status["ok"])
        self.assertEqual(status["data"]["app_version"], "test")
        self.module._log_path.write_text(json.dumps({
            "event": "manager_state", "timestamp": "2026-11-03T00:00:00Z",
            "result": "started", "title": "private", "pid": 123,
        }) + "\n", encoding="utf-8")
        logs = self.module._handle_request(self._request("logs", {"limit": 5000}))
        self.assertTrue(logs["ok"])
        self.assertEqual(logs["data"]["events"], [{
            "event": "manager_state", "timestamp": "2026-11-03T00:00:00Z",
            "result": "started",
        }])
        self.assertFalse(logs["data"]["content_recorded"])

    def test_logs_preserve_sanitized_monitor_failure_reason(self):
        self.module._log_path.write_text(json.dumps({
            "event": "topology_state", "timestamp": "2026-11-03T00:00:00Z",
            "result": "unavailable", "topology": "unknown", "output_count": 0,
            "reason": "topology_provider_exception", "provider_error_type": "TimeoutError",
            "monitor_identity_age_ms": 5200, "monitor_identity_cache_hit": True,
            "display_count": 1, "identified_display_count": 0,
            "monitor_path": "private", "error": "private text",
        }) + "\n", encoding="utf-8")
        logs = self.module._handle_request(self._request("logs", {"limit": 10}))
        row = logs["data"]["events"][0]
        self.assertEqual(row["reason"], "topology_provider_exception")
        self.assertEqual(row["provider_error_type"], "TimeoutError")
        self.assertEqual(row["monitor_identity_age_ms"], 5200)
        self.assertTrue(row["monitor_identity_cache_hit"])
        self.assertNotIn("monitor_path", row)
        self.assertNotIn("error", row)

    def test_screens_report_live_topology_without_capturing_pixels(self):
        result = self.module._handle_request(self._request("screens", {}))
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["state"], "verified")
        self.assertEqual(result["data"]["display_count"], 2)
        self.assertEqual(result["data"]["displays"][1]["virtual"], True)
        self.assertNotIn("monitor_path", result["data"]["displays"][1])
        self.assertEqual(result["data"]["output_reconciliation"]["state"],
                         "unmanaged_mtt_output")
        self.assertEqual(result["data"]["output_reconciliation"]["mtt_output_count"], 1)
        self.assertFalse(result["data"]["output_reconciliation"]["cleanup_eligible"])
        self.assertFalse(result["data"]["pixels_captured"])
        self.topology["topology_verified"] = False
        uncertain = self.module._handle_request(self._request("screens", {}))
        self.assertEqual(uncertain["data"]["state"], "unverified")
        self.assertFalse(uncertain["data"]["topology_verified"])
        self.assertEqual(uncertain["data"]["output_reconciliation"]["state"], "unverified")

    def test_output_reconciliation_is_clean_only_with_zero_mtt_outputs(self):
        self.topology["displays"] = self.topology["displays"][:1]
        result = self.module._handle_request(self._request("screens", {}))
        report = result["data"]["output_reconciliation"]
        self.assertEqual(report["state"], "clean")
        self.assertEqual(report["mtt_output_count"], 0)
        self.assertFalse(report["cleanup_eligible"])

    def test_output_reconciliation_ignores_star_desk_virtual_output(self):
        self.topology["displays"].append({
            "display_name": "\\\\.\\DISPLAY64", "target_name": "StarDesk Virtual Display",
            "width": 1920, "height": 1080, "refresh_rate": 60, "virtual": True,
            "attached": True, "verified": True, "topology_verified": True,
            "cloned": False, "mirroring_driver": False, "primary": False,
            "monitor_identity_verified": True, "monitor_parent": "ROOT\\DISPLAY\\0001",
        })
        result = self.module._handle_request(self._request("screens", {}))
        report = result["data"]["output_reconciliation"]
        self.assertEqual(report["state"], "unmanaged_mtt_output")
        self.assertEqual(report["mtt_output_count"], 1)

    def test_output_reconciliation_fails_closed_for_unknown_virtual_parent(self):
        self.topology["displays"][1].pop("monitor_parent")
        result = self.module._handle_request(self._request("screens", {}))
        report = result["data"]["output_reconciliation"]
        self.assertEqual(report["state"], "unverified")
        self.assertEqual(report["reason"], "virtual_parent_unavailable")
        self.assertFalse(report["cleanup_eligible"])

    def test_output_reconciliation_never_calls_an_active_lease_clean(self):
        self.pool = {"state": "active", "leases": 1, "output_count": 2,
                     "idle_remaining_seconds": 1200}
        result = self.module._handle_request(self._request("screens", {}))
        report = result["data"]["output_reconciliation"]
        self.assertEqual(report["state"], "active_lease")
        self.assertEqual(report["lease_count"], 1)
        self.assertFalse(report["cleanup_eligible"])

    def test_windows_list_is_content_free_and_bounded(self):
        result = self.module._handle_request(self._request("windows", {}))
        self.assertTrue(result["ok"])
        data = result["data"]
        self.assertEqual(data["windows"][0]["pid"], 321)
        self.assertNotIn("title", data["windows"][0])
        self.assertEqual(data["virtual_outputs"][0]["width"], 1920)
        self.assertTrue(data["movement_requires_confirmation"])
        self.assertFalse(data["pixels_captured"])

    def test_windows_busy_reader_returns_stable_code_without_exception_text(self):
        self.module._window_reader = lambda: (_ for _ in ()).throw(
            RuntimeError("Another manager operation is already active"))
        result = self.module._handle_request(self._request("windows", {}))
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["state"], "unavailable")
        self.assertEqual(result["data"]["error_code"], "MANAGER_BUSY")
        self.assertNotIn("Another manager operation", json.dumps(result))

    def test_windows_unknown_errors_keep_type_only_error_code(self):
        self.module._window_reader = lambda: (_ for _ in ()).throw(
            RuntimeError("private detail must not cross the pipe"))
        result = self.module._handle_request(self._request("windows", {}))
        self.assertEqual(result["data"]["error_code"], "RuntimeError")
        self.assertNotIn("private detail", json.dumps(result))

    def test_malformed_stale_unknown_and_replayed_requests_fail_closed(self):
        malformed = self._request("status", {})
        malformed["extra"] = True
        self.assertEqual(self.module._handle_request(malformed)["error_code"], "REQUEST_INVALID")
        stale = self._request("status", {})
        stale["issued_at_ms"] -= 61_000
        self.assertEqual(self.module._handle_request(stale)["error_code"], "REQUEST_INVALID")
        unknown = self.module._handle_request(self._request("execute", {"command": "whoami"}))
        self.assertEqual(unknown["error_code"], "REQUEST_INVALID")
        unhashable = self._request("status", {})
        unhashable["command"] = []
        self.assertEqual(self.module._handle_request(unhashable)["error_code"], "REQUEST_INVALID")
        request = self._request("status", {})
        self.assertTrue(self.module._handle_request(request)["ok"])
        self.assertEqual(self.module._handle_request(request)["error_code"], "REQUEST_REPLAYED")

    def test_output_plan_requires_exact_phrase_and_fresh_state(self):
        preview = self.module._handle_request(self._request(
            "outputs.preview", {"count": 2, "profile": "high"}))
        self.assertTrue(preview["ok"])
        data = preview["data"]
        self.assertEqual(data["total_mtt_outputs"], 3)
        self.assertEqual(data["mode"], [2560, 1440, 60])
        self.assertEqual(data["mode_evidence"], "active_mtt_outputs")
        wrong = self.module._handle_request(self._request("outputs.apply", {
            "plan_id": data["plan_id"], "confirm": "CREATE 2 ULTRA"}))
        self.assertEqual(wrong["error_code"], "CONFIRMATION_MISMATCH")
        self.assertEqual(self.begin_calls, [])
        preview = self.module._handle_request(self._request(
            "outputs.preview", {"count": 2, "profile": "high"}))
        data = preview["data"]
        self.topology["displays"][1]["width"] = 1280
        stale = self.module._handle_request(self._request("outputs.apply", {
            "plan_id": data["plan_id"], "confirm": data["confirmation"]}))
        self.assertEqual(stale["error_code"], "LIVE_STATE_CHANGED")
        self.assertEqual(self.begin_calls, [])

    def test_zero_output_preview_uses_labeled_vdd_configuration_evidence(self):
        self.topology["displays"] = self.topology["displays"][:1]
        preview = self.module._handle_request(self._request(
            "outputs.preview", {"count": 2, "profile": "high"}))
        self.assertTrue(preview["ok"])
        data = preview["data"]
        self.assertEqual(data["existing_mtt_outputs"], 0)
        self.assertEqual(data["total_mtt_outputs"], 2)
        self.assertEqual(data["mode"], [2560, 1440, 60])
        self.assertEqual(data["mode_evidence"], "configured_vdd_settings")
        self.assertEqual(self.begin_calls, [])

        self.configured_mode_evidence["sha256"] = "e" * 64
        applied = self.module._handle_request(self._request("outputs.apply", {
            "plan_id": data["plan_id"], "confirm": data["confirmation"]}))
        self.assertEqual(applied["error_code"], "LIVE_STATE_CHANGED")
        self.assertEqual(self.begin_calls, [])

    def test_zero_output_preview_fails_closed_for_missing_or_unsupported_config(self):
        self.topology["displays"] = self.topology["displays"][:1]
        self.module._configured_mode_reader = lambda: (_ for _ in ()).throw(OSError("private path"))
        unavailable = self.module._handle_request(self._request(
            "outputs.preview", {"count": 2, "profile": "high"}))
        self.assertEqual(unavailable["error_code"], "OUTPUT_MODE_EVIDENCE_UNAVAILABLE")

        self.module._configured_mode_reader = lambda: {
            "modes": [[1920, 1080, 60]], "sha256": "a" * 64}
        unsupported = self.module._handle_request(self._request(
            "outputs.preview", {"count": 2, "profile": "high"}))
        self.assertEqual(unsupported["error_code"], "PROFILE_UNSUPPORTED")

    def test_vdd_settings_parser_accepts_declared_modes_and_rejects_unsafe_xml(self):
        raw = (b"<vdd_settings><global><g_refresh_rate>60</g_refresh_rate></global>"
               b"<resolutions><resolution><width>2560</width><height>1440</height>"
               b"</resolution></resolutions></vdd_settings>")
        self.assertEqual(_parse_vdd_settings_modes(raw), [[2560, 1440, 60]])
        for invalid in (
                b"<vdd_settings><resolutions><resolution><width>0</width><height>1080</height>"
                b"<refresh_rate>60</refresh_rate></resolution></resolutions></vdd_settings>",
                b"<!DOCTYPE x [<!ENTITY e SYSTEM 'file:///secret'>]><vdd_settings/>",
                "<!DOCTYPE x [<!ENTITY e SYSTEM 'file:///secret'>]><vdd_settings/>".encode("utf-16-le"),
                b"x" * 262_145):
            with self.subTest(invalid=invalid[:32]):
                with self.assertRaisesRegex(ValueError, "OUTPUT_MODE_EVIDENCE_UNAVAILABLE"):
                    _parse_vdd_settings_modes(invalid)

    def test_output_plan_ignores_capture_timestamp_but_rejects_real_state_change(self):
        preview = self.module._handle_request(self._request(
            "outputs.preview", {"count": 2, "profile": "high"}))
        self.assertTrue(preview["ok"])
        data = preview["data"]

        # Refresh metadata changes on every live identity probe.
        self.topology["captured_at_unix_ms"] = 2_000_000
        self.topology["monitor_identity_age_ms"] = 148
        self.topology["monitor_identity_cache_hit"] = True
        applied = self.module._handle_request(self._request("outputs.apply", {
            "plan_id": data["plan_id"], "confirm": data["confirmation"]}))
        self.assertTrue(applied["ok"])
        self.assertEqual(len(self.begin_calls), 1)

        # A new plan must still be invalidated by an endpoint identity change.
        preview = self.module._handle_request(self._request(
            "outputs.preview", {"count": 2, "profile": "high"}))
        data = preview["data"]
        self.topology["displays"][1]["monitor_path"] = "MTT/REPLACED"
        stale = self.module._handle_request(self._request("outputs.apply", {
            "plan_id": data["plan_id"], "confirm": data["confirmation"]}))
        self.assertEqual(stale["error_code"], "LIVE_STATE_CHANGED")
        self.assertEqual(len(self.begin_calls), 1)

    def test_output_apply_is_confirmed_single_use_and_profile_bounded(self):
        preview = self.module._handle_request(self._request(
            "outputs.preview", {"count": 2, "profile": "high"}))
        data = preview["data"]
        applied = self.module._handle_request(self._request("outputs.apply", {
            "plan_id": data["plan_id"], "confirm": data["confirmation"]}))
        self.assertTrue(applied["ok"])
        self.assertEqual(self.begin_calls[0]["output_count"], 3)
        self.assertEqual(self.begin_calls[0]["profile"], "high")
        self.assertTrue(self.begin_calls[0]["confirmed"])
        replay = self.module._handle_request(self._request("outputs.apply", {
            "plan_id": data["plan_id"], "confirm": data["confirmation"]}))
        self.assertEqual(replay["error_code"], "PLAN_EXPIRED_OR_UNKNOWN")
        self.assertEqual(len(self.begin_calls), 1)

    def test_output_apply_exposes_only_allowlisted_runtime_failure_codes(self):
        preview = self.module._handle_request(self._request(
            "outputs.preview", {"count": 2, "profile": "high"}))
        data = preview["data"]

        def missing_physical_primary(_payload):
            raise RuntimeError("PHYSICAL_PRIMARY_UNVERIFIED")

        self.module._pool_begin = missing_physical_primary
        blocked = self.module._handle_request(self._request("outputs.apply", {
            "plan_id": data["plan_id"], "confirm": data["confirmation"]}))
        self.assertEqual(blocked["error_code"], "PHYSICAL_PRIMARY_UNVERIFIED")
        self.assertEqual(self.begin_calls, [])

        preview = self.module._handle_request(self._request(
            "outputs.preview", {"count": 2, "profile": "high"}))
        data = preview["data"]

        def private_error(_payload):
            raise RuntimeError(r"C:\private\window-title=secret")

        self.module._pool_begin = private_error
        hidden = self.module._handle_request(self._request("outputs.apply", {
            "plan_id": data["plan_id"], "confirm": data["confirmation"]}))
        self.assertEqual(hidden["error_code"], "MANAGER_COMMAND_FAILED")
        self.assertNotIn("private", json.dumps(hidden))
        self.assertNotIn("secret", json.dumps(hidden))

    def test_return_requires_preview_and_does_not_call_stop_on_changed_state(self):
        self.pool = {"state": "active", "leases": 0}
        preview = self.module._handle_request(self._request("outputs.return_preview", {}))
        data = preview["data"]
        self.topology["displays"][1]["width"] = 1280
        result = self.module._handle_request(self._request("outputs.return", {
            "plan_id": data["plan_id"], "confirm": data["confirmation"]}))
        self.assertEqual(result["error_code"], "LIVE_STATE_CHANGED")
        self.assertEqual(self.stop_calls, [])

    def test_uncertain_or_cloned_topology_never_creates_plan(self):
        self.topology["displays"][1]["cloned"] = True
        result = self.module._handle_request(self._request(
            "outputs.preview", {"count": 2, "profile": "high"}))
        self.assertEqual(result["error_code"], "TOPOLOGY_UNVERIFIED")
        self.assertEqual(self.module._plans, {})

    def test_dispose_stops_owned_server_and_clears_confirmation_plans(self):
        self.module._plans["c" * 32] = {"kind": "return"}
        self.module.dispose()
        self.assertTrue(self.server.stopped)
        self.assertEqual(self.module._plans, {})

    def test_shutdown_pause_refuses_live_cli_request_and_can_resume(self):
        self.server.active_clients = 1
        self.assertFalse(self.module.pause_for_shutdown())
        self.assertTrue(self.module._accepting)
        self.server.active_clients = 0
        self.assertTrue(self.module.pause_for_shutdown())
        self.assertFalse(self.module._accepting)
        self.assertTrue(self.module.resume_after_shutdown())
        self.assertTrue(self.module._accepting)
        self.assertTrue(self.server.ready)

    def test_startup_log_exposes_error_type_but_not_message_or_path(self):
        path = self.module._log_path.parent / f"startup-{uuid.uuid4().hex}.json"
        try:
            path.write_text(json.dumps({
                "error": "TimeoutError: C:/private/token=secret private exception text",
                "at": 1_700_000_000,
                "message": "C:/private/token=secret",
            }), encoding="utf-8")
            report = self.module._startup_log(path)
            self.assertEqual(report, {"state": "ready", "present": True,
                                      "error_type": "TimeoutError", "timestamp": 1_700_000_000})
            self.assertNotIn("secret", json.dumps(report))
        finally:
            path.unlink(missing_ok=True)

    def test_startup_log_accepts_new_bare_error_type(self):
        path = self.module._log_path.parent / f"startup-{uuid.uuid4().hex}.json"
        try:
            path.write_text(json.dumps({"error": "TimeoutError", "at": 1_700_000_000}),
                            encoding="utf-8")
            report = self.module._startup_log(path)
            self.assertEqual(report, {"state": "ready", "present": True,
                                      "error_type": "TimeoutError", "timestamp": 1_700_000_000})
        finally:
            path.unlink(missing_ok=True)

    def test_startup_log_rejects_untyped_text(self):
        path = self.module._log_path.parent / f"startup-{uuid.uuid4().hex}.json"
        try:
            path.write_text(json.dumps({"error": "private text without type", "at": 1_700_000_000}),
                            encoding="utf-8")
            self.assertEqual(self.module._startup_log(path), {
                "state": "unavailable", "error_code": "STARTUP_LOG_INVALID"})
        finally:
            path.unlink(missing_ok=True)

    def test_task_cli_allows_only_registered_private_targets(self):
        denied = self.module._handle_request(self._request("task.start", {
            "confirmed": True, "output_count": 2, "profile": "high", "target_app_id": "other"}))
        self.assertEqual(denied["error_code"], "ARGUMENT_INVALID")
        not_confirmed = self.module._handle_request(self._request("task.start", {
            "confirmed": False, "output_count": 2, "profile": "high"}))
        self.assertEqual(not_confirmed["error_code"], "CONFIRMATION_REQUIRED")
        started = self.module._handle_request(self._request("task.start", {
            "confirmed": True, "output_count": 0, "profile": "private"}))
        self.assertTrue(started["ok"])
        action, payload = self.task_calls[-1]
        self.assertEqual(action, "start")
        self.assertEqual(payload["target_app_id"], "only_chat_qa")
        self.assertEqual(payload["output_request"], {"output_count": 0, "profile": "private"})
        self.assertEqual(set(payload), {"protocol_version", "request_id", "action",
                                        "target_app_id", "confirmed", "output_request"})
        router_started = self.module._handle_request(self._request("task.start", {
            "confirmed": True, "output_count": 0, "profile": "private",
            "target_app_id": "ai_account_router_qa"}))
        self.assertTrue(router_started["ok"])
        action, payload = self.task_calls[-1]
        self.assertEqual(action, "start")
        self.assertEqual(payload["target_app_id"], "ai_account_router_qa")
        self.assertEqual(payload["output_request"], {"output_count": 0, "profile": "private"})

    def test_task_cli_rejects_host_vdd_before_dispatch(self):
        count = len(self.task_calls)
        rejected = self.module._handle_request(self._request("task.start", {
            "confirmed": True, "output_count": 2, "profile": "high"}))
        self.assertEqual(rejected["error_code"], "OUTPUT_REQUEST_INVALID")
        self.assertEqual(len(self.task_calls), count)

    def test_task_observation_allows_bounded_runtime_capture_reply(self):
        self.module._task_action = lambda _action, payload: {
            "state": "running", "request_id": payload["request_id"],
            "packet_base64": "A" * (2 * 1024 * 1024)}
        result = self.module._handle_request(self._request("task.observe", {
            "session_id": "00000000-0000-4000-8000-000000000001"}))
        self.assertTrue(result["ok"])

    def test_real_runtime_binary_packet_uses_bounded_json_wire_format(self):
        import base64
        from core.runtime_packet import encode_runtime_packet
        from core.png_capture import encode_bgra_png
        sid = "00000000-0000-4000-8000-000000000001"
        packet = encode_runtime_packet({"protocol_version": 1, "action": "observe",
            "request_id": sid, "session_id": sid}, encode_bgra_png(bytes((0, 0, 255, 255)) * 4, 2, 2))
        self.module._task_action = lambda _action, payload: {"state": "running", "packet": packet}
        result = self.module._handle_request(self._request("task.observe", {"session_id": sid}))
        self.assertTrue(result["ok"])
        self.assertNotIn("packet", result["data"])
        self.assertEqual(base64.b64decode(result["data"]["packet_base64"], validate=True), packet)
        self.module._task_action = lambda _action, payload: {"packet": b""}
        rejected = self.module._handle_request(self._request("task.observe", {"session_id": sid}))
        self.assertEqual(rejected["error_code"], "OBSERVATION_PACKET_INVALID")


if __name__ == "__main__":
    unittest.main(verbosity=2)
