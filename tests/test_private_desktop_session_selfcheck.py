from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


SESSION_ID = "11111111-1111-4111-8111-111111111111"
PNG_ONE = app.PNG_SIGNATURE + b"first-frame"
PNG_TWO = app.PNG_SIGNATURE + b"updated-frame"


class FakeModuleHost:
    def __init__(self, _root, observations):
        self.observations = list(observations)
        self.stop_calls = 0
        self.dispose_calls = 0
        self.session_active = False

    def load(self, module_id):
        if module_id == "private_desktop":
            return {"version": "test", "sha256": "a" * 64}
        return {"version": "test", "sha256": "b" * 64}

    def invoke(self, module_id, action, payload):
        if action == "start_diagnostic_session":
            if self.session_active:
                raise RuntimeError("active diagnostic session already exists")
            self.session_active = True
            return {
                "started": True,
                "session_id": SESSION_ID,
                "window_absent_from_input_desktop": True,
                "input_desktop_unchanged": True,
                "display_enabled": False,
            }
        if action == "observe_session":
            if payload["session_id"] != SESSION_ID:
                raise ValueError("stale diagnostic session ID")
            if not self.observations:
                return {"error_code": "capture_content_invalid"}
            return self.observations.pop(0)
        if action == "invoke_diagnostic_button":
            return {
                "passed": True,
                "window_found": True,
                "button_found": True,
                "button_invoked": True,
                "status_verified": True,
                "host_foreground_unchanged": True,
                "target_absent_from_input_desktop": True,
                "input_desktop_unchanged": True,
            }
        if action == "stop_session":
            self.stop_calls += 1
            self.session_active = False
            return {"stopped": True, "desktop_removed": True,
                    "input_desktop_unchanged": True}
        raise AssertionError(f"Unexpected private desktop action: {action}")

    def snapshot(self, _module_id):
        raise RuntimeError("reload is deferred while diagnostic session is active")

    def refresh(self, module_id):
        if module_id == "only_chat_debug_display":
            return {"lease_acquisition": "fail_closed", "display_enabled": False}
        return {"diagnostic_session": "active" if self.session_active else "inactive"}

    def dispose_all(self):
        self.dispose_calls += 1
        self.session_active = False


def observation(png: bytes):
    return {
        "session_state": "active",
        "window_id": 123,
        "width": 450,
        "height": 238,
        "input_desktop_unchanged": True,
        "png_sha256": hashlib.sha256(png).hexdigest(),
        "png_bytes": base64.b64encode(png).decode("ascii"),
    }


class PrivateDesktopSessionSelfCheckTests(unittest.TestCase):
    def run_self_check(self, observations):
        host = FakeModuleHost(None, observations)
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "session-check.json"
            with patch.object(app, "ModuleHost", return_value=host), \
                    patch.object(app.time, "sleep", return_value=None):
                result = app.run_private_desktop_session_self_check(str(report))
            data = json.loads(report.read_text(encoding="utf-8"))
        return result, data, host

    def test_retries_initial_paint_and_waits_for_post_action_frame(self):
        result, report, host = self.run_self_check([
            {"error_code": "capture_content_invalid"},
            observation(PNG_ONE),
            observation(PNG_ONE),
            {"error_code": "capture_content_invalid"},
            observation(PNG_TWO),
        ])

        self.assertEqual(result, 0, report)
        self.assertTrue(report["session"]["passed"])
        self.assertEqual(report["session"]["observe_before_capture_attempts"], 2)
        self.assertEqual(report["session"]["observe_after_capture_attempts"], 3)
        self.assertTrue(report["session"]["observe_before"]["png_hash_matches"])
        self.assertTrue(report["session"]["observe_after"]["frame_changed_after_action"])
        self.assertNotIn("png_bytes", json.dumps(report))
        self.assertEqual(host.stop_calls, 1)
        self.assertEqual(host.dispose_calls, 1)
        self.assertFalse(host.session_active)

    def test_initial_frame_timeout_is_bounded_and_disposes_session(self):
        host = FakeModuleHost(None, [{"error_code": "capture_content_invalid"}] * 4)
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "session-check.json"
            with patch.object(app, "ModuleHost", return_value=host), \
                    patch.object(app, "PRIVATE_DESKTOP_FRAME_MAX_ATTEMPTS", 3), \
                    patch.object(app.time, "sleep", return_value=None):
                result = app.run_private_desktop_session_self_check(str(report))
            data = json.loads(report.read_text(encoding="utf-8"))

        self.assertEqual(result, 1)
        self.assertEqual(data["error"], "RuntimeError: PRIVATE_DESKTOP_INITIAL_FRAME_UNAVAILABLE")
        self.assertEqual(data["session"]["observe_before_capture_attempts"], 3)
        self.assertEqual(host.stop_calls, 1)
        self.assertEqual(host.dispose_calls, 1)
        self.assertFalse(host.session_active)

    def test_nontransient_capture_error_is_not_retried(self):
        result, report, host = self.run_self_check([
            {"error_code": "HOST_INPUT_DESKTOP_CHANGED"},
        ])

        self.assertEqual(result, 1)
        self.assertEqual(report["error"], "RuntimeError: PRIVATE_DESKTOP_INITIAL_FRAME_UNAVAILABLE")
        self.assertEqual(report["session"]["observe_before_capture_attempts"], 1)
        self.assertEqual(host.stop_calls, 1)
        self.assertEqual(host.dispose_calls, 1)
        self.assertFalse(host.session_active)
