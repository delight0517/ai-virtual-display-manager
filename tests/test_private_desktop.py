from __future__ import annotations

import sys
import os
import uuid
import ctypes
import types
import struct
import threading
import unittest
import zlib
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.module_host import ModuleHost
from modules.private_desktop.module import (PrivateDesktopModule, _ExtendedLimitInformation,
                                            _ProcessInformation, _StartupInfo, _Win32,
                                            PrivateDesktopCaptureError, _Rect,
                                            _mark_uncreated_probe_resources,
                                            _verify_desktop_removal)
from modules.private_desktop.session import (DiagnosticSession,
                                             DiagnosticSessionStartError,
                                             _input_desktop_interruption,
                                             _matches_window_class_pattern,
                                             _startup_error_code,
                                             _window_exists,
                                             _window_absent_from_input_desktop,
                                             startup_resources_absent_verified)
from core.png_capture import (MAX_CAPTURE_PIXELS, MAX_PNG_BYTES, PNG_SIGNATURE,
                              encode_bgra_png, require_contentful_bgra_capture,
                              sampled_bgra_color_count,
                              validate_encoded_png)
from app import private_desktop_summary


class PrivateDesktopModuleTests(unittest.TestCase):
    def test_window_class_patterns_are_exact_or_bounded_prefixes(self):
        prefix = "WindowsForms10.Window.8.app.0."
        self.assertTrue(_matches_window_class_pattern(
            prefix + "34f5582_r7_ad1", prefix + "*"))
        self.assertFalse(_matches_window_class_pattern("UnrelatedWindow", prefix + "*"))
        self.assertTrue(_matches_window_class_pattern("PinnedWindow", "PinnedWindow"))
        for invalid in ("*", "Short*", "WindowsForms10.*.app.0", prefix + "**"):
            with self.subTest(pattern=invalid):
                self.assertFalse(_matches_window_class_pattern(prefix + "suffix", invalid))

    def setUp(self):
        self.result = {
            "passed": True,
            "display_enabled": False,
            "window_message_roundtrip": True,
            "capture_nonblank": True,
            "window_absent_from_input_desktop": True,
            "input_desktop_unchanged": True,
            "cleanup": {"process_stopped": True, "desktop_thread_restored": True,
                        "desktop_handle_closed": True, "desktop_removed": True,
                        "job_handle_closed": True},
        }
        self.runner = Mock(return_value=self.result)
        self.module = PrivateDesktopModule(self.runner)
        self.module.init()

    def test_private_target_window_revalidation_pins_the_registered_class(self):
        from ctypes import wintypes
        class User32:
            def EnumDesktopWindows(self, _desktop, callback, param):
                callback(123, param)
                return True

            @staticmethod
            def GetWindowTextW(_hwnd, buffer, _size):
                buffer.value = "Target window"
                return len(buffer.value)

            @staticmethod
            def GetClassNameW(_hwnd, buffer, _size):
                buffer.value = "PinnedWindowClass"
                return len(buffer.value)

            @staticmethod
            def GetWindowThreadProcessId(_hwnd, pid_ptr):
                ctypes.cast(pid_ptr, ctypes.POINTER(wintypes.DWORD))[0] = 777
                return 888

        api = type("WindowApi", (), {
            "CALLBACK": ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                           wintypes.LPARAM),
            "user32": User32(),
        })()
        self.assertTrue(_window_exists(api, 1, 123, 777, None,
                                       "PinnedWindowClass"))
        self.assertFalse(_window_exists(api, 1, 123, 777, None,
                                        "DifferentWindowClass"))

    def tearDown(self):
        if self.module._initialized and not self.module._busy:
            self.module.dispose()


    def test_status_is_truthful_and_does_not_enable_consumer_lease(self):
        state = self.module.refresh()
        self.assertEqual(state["backend"], "windows_private_desktop_probe")
        self.assertEqual(state["consumer_launch"], "not_implemented")
        self.assertEqual(state["consumer_lease"], "fail_closed")
        self.assertEqual(state["probe"], "not_run")
        text = private_desktop_summary(state)
        self.assertIn("Only Chat/Codex 실행 연결", text)

    def test_probe_requires_exact_confirmation_and_does_not_call_runner(self):
        for confirmation in (None, False, 1, "true"):
            with self.subTest(confirmation=confirmation), self.assertRaises(ValueError):
                self.module.action("probe", {"confirmed": confirmation})
        self.runner.assert_not_called()

    def test_probe_result_is_saved_and_restored(self):
        result = self.module.action("probe", {"confirmed": True})
        self.assertIs(result, self.result)
        self.runner.assert_called_once_with()
        self.assertEqual(self.module.snapshot()["last_probe"], self.result)
        restored = PrivateDesktopModule(Mock())
        restored.restore(self.module.snapshot())
        self.assertEqual(restored.snapshot()["last_probe"], self.result)
        restored.dispose()

    def test_probe_and_dispose_are_guarded_during_active_run(self):
        entered, release = threading.Event(), threading.Event()

        def slow_probe():
            entered.set()
            release.wait(2)
            return self.result

        module = PrivateDesktopModule(slow_probe)
        module.init()
        errors = []
        thread = threading.Thread(target=lambda: module.action("probe", {"confirmed": True}))
        thread.start()
        self.assertTrue(entered.wait(1))
        with self.assertRaisesRegex(RuntimeError, "probe is active"):
            module.snapshot()
        with self.assertRaisesRegex(RuntimeError, "probe is active"):
            module.dispose()
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        module.dispose()

    def test_failed_probe_is_not_promoted_to_success(self):
        failed = {"passed": False, "error_code": "PrintWindow returned blank"}
        module = PrivateDesktopModule(lambda: failed)
        module.init()
        self.assertIs(module.action("probe", {"confirmed": True}), failed)
        self.assertIs(module.snapshot()["last_probe"]["passed"], False)
        module.dispose()

    def test_registered_module_loads_through_versioned_host(self):
        host = ModuleHost(ROOT)
        try:
            info = host.load("private_desktop")
            self.assertEqual(info["version"], "0.1.21")
            self.assertEqual(host.refresh("private_desktop")["consumer_lease"], "fail_closed")
            self.assertEqual(host.invoke("private_desktop", "status")["probe"], "not_run")
        finally:
            host.dispose_all()

    def test_diagnostic_session_actions_are_confirmation_and_identity_scoped(self):
        fake_session = Mock()
        fake_session.session_id = "session-123"
        fake_session.desktop_name = "AIVDDS_test"
        fake_session.active = True
        fake_session._state = {"input_desktop_unchanged": True,
                               "window_absent_from_input_desktop": True}
        fake_session.request.side_effect = [
            {"session_state": "active", "png_bytes": "synthetic"},
            {"passed": True},
            {"stopped": True, "desktop_removed": True},
        ]
        with patch("modules.private_desktop.session.DiagnosticSession",
                   return_value=fake_session):
            started = self.module.action("start_diagnostic_session", {"confirmed": True})
        self.assertTrue(started["started"])
        self.assertFalse(started["display_enabled"])
        with self.assertRaisesRegex(ValueError, "stale"):
            self.module.action("observe_session", {"session_id": "wrong"})
        observed = self.module.action("observe_session", {"session_id": "session-123"})
        self.assertEqual(observed["session_state"], "active")
        self.assertEqual(fake_session.request.call_args.args, ("observe",))
        self.assertTrue(self.module.action("invoke_diagnostic_button",
                                           {"session_id": "session-123"})["passed"])
        stopped = self.module.action("stop_session", {"session_id": "session-123"})
        self.assertTrue(stopped["desktop_removed"])
        self.assertEqual(self.module.refresh()["diagnostic_session"], "inactive")
        with self.assertRaises(ValueError):
            self.module.action("start_diagnostic_session", {"confirmed": 1})

    def test_active_diagnostic_session_rejects_reload_snapshot(self):
        fake_session = Mock()
        fake_session.session_id = "session-123"
        fake_session.desktop_name = "AIVDDS_test"
        fake_session.active = True
        fake_session._state = {"input_desktop_unchanged": True,
                               "window_absent_from_input_desktop": True}
        with patch("modules.private_desktop.session.DiagnosticSession",
                   return_value=fake_session):
            self.module.action("start_diagnostic_session", {"confirmed": True})
        with self.assertRaisesRegex(RuntimeError, "reload is deferred"):
            self.module.snapshot()
        fake_session.close.return_value = {"stopped": True}
        self.module.dispose()
        self.assertTrue(fake_session.close.called)

    def test_observation_fails_closed_when_input_desktop_changes(self):
        api = Mock()
        api.input_desktop_name.return_value = "Winlogon"
        result = DiagnosticSession._observe(api, 100, "Default")
        self.assertEqual(result["session_state"], "interrupted")
        self.assertEqual(result["error_code"], "HOST_INPUT_DESKTOP_CHANGED")
        api.capture_png.assert_not_called()

    def test_capture_reports_printwindow_stage_without_exposing_os_error_text(self):
        api = _Win32.__new__(_Win32)
        user32, gdi32 = Mock(), Mock()
        api.user32, api.gdi32 = user32, gdi32
        user32.SetThreadDpiAwarenessContext.return_value = 1
        user32.GetWindowRect.side_effect = lambda _hwnd, rect_ptr: (
            setattr(ctypes.cast(rect_ptr, ctypes.POINTER(_Rect)).contents, "right", 2),
            setattr(ctypes.cast(rect_ptr, ctypes.POINTER(_Rect)).contents, "bottom", 2),
            True)[-1]
        user32.GetDC.return_value = 10
        gdi32.CreateCompatibleDC.return_value = 11
        pixels = ctypes.create_string_buffer(16)

        def create_bitmap(_screen, _info, _usage, bits_ptr, _section, _offset):
            ctypes.cast(bits_ptr, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.cast(
                pixels, ctypes.c_void_p)
            return 12

        gdi32.CreateDIBSection.side_effect = create_bitmap
        gdi32.SelectObject.return_value = 13
        user32.PrintWindow.return_value = 0
        with self.assertRaises(PrivateDesktopCaptureError) as caught:
            api.capture_png(99)
        self.assertEqual(caught.exception.diagnostic_code,
                         "capture_print_window_failed")
        self.assertEqual(str(caught.exception), "capture_print_window_failed")
        user32.ReleaseDC.assert_called_once_with(None, 10)
        gdi32.DeleteDC.assert_called_once_with(11)

    def test_session_input_desktop_watch_detects_secure_desktop_and_unknown(self):
        api = Mock()
        api.input_desktop_name.return_value = "Winlogon"
        changed = _input_desktop_interruption(api, "Default")
        self.assertEqual(changed["error_code"], "HOST_INPUT_DESKTOP_CHANGED")
        self.assertEqual(changed["input_desktop_current"], "Winlogon")
        api.input_desktop_name.side_effect = OSError("access denied")
        unknown = _input_desktop_interruption(api, "Default")
        self.assertEqual(unknown["error_code"], "HOST_INPUT_DESKTOP_UNKNOWN")
        self.assertEqual(unknown["input_desktop_current"], "unknown")
        api.input_desktop_name.side_effect = None
        api.input_desktop_name.return_value = "Default"
        self.assertIsNone(_input_desktop_interruption(api, "Default"))

    def test_startup_error_code_reports_only_known_input_desktop_preflight_failures(self):
        denied = PermissionError(5, "Access is denied")
        self.assertEqual(_startup_error_code("input_desktop_preflight", denied),
                         "HOST_INPUT_DESKTOP_ACCESS_DENIED")
        self.assertEqual(_startup_error_code(
            "input_desktop_preflight", RuntimeError("HOST_INPUT_DESKTOP_UNKNOWN")),
            "HOST_INPUT_DESKTOP_UNKNOWN")
        self.assertEqual(_startup_error_code(
            "create_private_desktop", denied), "PRIVATE_DESKTOP_START_FAILED")
        self.assertEqual(_startup_error_code(
            "input_desktop_preflight", OSError("access denied")),
            "PRIVATE_DESKTOP_START_FAILED")

    def test_focus_safety_gate_uses_exact_target_absence_from_input_desktop(self):
        api = Mock()
        api.window_is_on_input_desktop.return_value = False
        self.assertTrue(_window_absent_from_input_desktop(api, 404))
        api.window_is_on_input_desktop.assert_called_once_with(404)
        api.window_is_on_input_desktop.return_value = True
        self.assertFalse(_window_absent_from_input_desktop(api, 404))
        api.window_is_on_input_desktop.side_effect = OSError("desktop query failed")
        self.assertFalse(_window_absent_from_input_desktop(api, 404))

    @unittest.skipUnless(sys.platform == "win32", "requires Win32 private desktops")
    def test_failed_session_start_keeps_baseline_and_reports_complete_cleanup(self):
        api = _Win32()
        try:
            baseline_desktop = api.input_desktop_name()
        except OSError as exc:
            self.skipTest(f"input desktop is not queryable without interaction: {type(exc).__name__}")
        if baseline_desktop != "Default":
            self.skipTest("requires a queryable Default input desktop; current desktop is not verified Default")
        api.probe_command = lambda _token: (r"C:\AIVDD_missing_test_target\missing.exe", [])
        created_desktops = []
        real_create_desktop = api.user32.CreateDesktopW

        def capture_desktop_name(name, *args):
            created_desktops.append(name)
            return real_create_desktop(name, *args)

        api.user32.CreateDesktopW = capture_desktop_name
        session = object.__new__(DiagnosticSession)
        with self.assertRaises(RuntimeError):
            session.__init__(api, _StartupInfo, _ProcessInformation,
                             _ExtendedLimitInformation)
        session._thread.join(timeout=8)
        receipt = session._state.get("stop_receipt", {})
        self.assertFalse(session._thread.is_alive())
        self.assertEqual(session._state.get("input_desktop_before"), baseline_desktop)
        self.assertTrue(created_desktops)
        self.assertTrue(receipt.get("stopped"))
        self.assertTrue(receipt.get("target_stopped"))
        self.assertTrue(receipt.get("desktop_handle_closed"))
        self.assertTrue(receipt.get("desktop_removed"))
        self.assertTrue(receipt.get("job_handle_closed"))
        self.assertTrue(receipt.get("input_desktop_unchanged"))
        self.assertEqual(api.input_desktop_name(), baseline_desktop)
        self.assertFalse(api.desktop_exists(created_desktops[0]))

    def test_constructor_raises_typed_error_only_after_verified_cleanup(self):
        receipt = {
            "stopped": True, "target_stopped": True,
            "desktop_thread_restored": True, "desktop_handle_closed": True,
            "desktop_removed": True, "owned_window_removed": True,
            "job_handle_closed": True,
            "input_desktop_unchanged": True,
        }

        def fail_before_win32_resources(session):
            session._state.update({
                "state": "closed", "error": "OpenInputDesktop failed",
                "stop_receipt": receipt,
            })
            session._ready.set()

        with patch.object(DiagnosticSession, "_owner", fail_before_win32_resources):
            with self.assertRaises(DiagnosticSessionStartError) as caught:
                DiagnosticSession(None, None, None, None)
        self.assertEqual(caught.exception.cleanup_receipt, receipt)

    def test_preflight_resource_absence_requires_explicit_false_for_every_resource(self):
        receipt = {"desktop_created": False, "job_created": False,
                   "process_created": False, "window_created": False,
                   "input_desktop_unchanged": False}
        self.assertTrue(startup_resources_absent_verified(receipt))
        self.assertFalse(startup_resources_absent_verified({**receipt, "job_created": True}))
        self.assertFalse(startup_resources_absent_verified({k: v for k, v in receipt.items()
                                                             if k != "process_created"}))

    def test_constructor_keeps_unverified_cleanup_as_generic_failure(self):
        def fail_with_incomplete_cleanup(session):
            session._state.update({
                "state": "closed", "error": "OpenInputDesktop failed",
                "stop_receipt": {
                    "stopped": True, "target_stopped": True,
                    "desktop_thread_restored": True,
                    "desktop_handle_closed": True,
                    "desktop_removed": False, "owned_window_removed": True,
                    "job_handle_closed": True,
                    "input_desktop_unchanged": True,
                },
            })
            session._ready.set()

        with patch.object(DiagnosticSession, "_owner", fail_with_incomplete_cleanup):
            with self.assertRaisesRegex(RuntimeError, "PRIVATE_DESKTOP_CLEANUP_FAILED") as caught:
                DiagnosticSession(None, None, None, None)
        self.assertNotIsInstance(caught.exception, DiagnosticSessionStartError)

    def test_timeout_cleanup_path_can_only_report_typed_failure_after_stop_receipt(self):
        receipt = {
            "stopped": True, "target_stopped": True,
            "desktop_thread_restored": True, "desktop_handle_closed": True,
            "desktop_removed": True, "owned_window_removed": True,
            "job_handle_closed": True, "input_desktop_unchanged": True,
        }

        def worker_waits_for_stop(session):
            action, response = session._commands.get(timeout=1)
            if action == "stop":
                session._state.update({"state": "closed", "stop_receipt": receipt})
                response.put({"stopping": True})

        original_start = threading.Thread.start

        def force_ready_timeout(thread):
            if thread.name == "private-desktop-session":
                thread._target.__self__._ready.wait = Mock(return_value=False)
            return original_start(thread)

        with patch.object(DiagnosticSession, "_owner", worker_waits_for_stop):
            with patch.object(threading.Thread, "start", force_ready_timeout):
                with self.assertRaises(DiagnosticSessionStartError) as caught:
                    DiagnosticSession(None, None, None, None)
        self.assertEqual(caught.exception.cleanup_receipt, receipt)

    def test_stop_retry_reuses_pending_command_and_returns_final_receipt(self):
        receipt = {
            "stopped": True, "target_stopped": True,
            "desktop_thread_restored": True, "desktop_handle_closed": True,
            "desktop_removed": True, "owned_window_removed": True,
            "job_handle_closed": True, "input_desktop_unchanged": True,
        }
        release_cleanup = threading.Event()

        def owner_waits_for_stop(session):
            session._state = {"state": "active"}
            session._ready.set()
            action, response = session._commands.get(timeout=2)
            if action == "stop":
                response.put({"stopping": True})
                release_cleanup.wait(timeout=2)
                session._state.update({"state": "closed", "stop_receipt": receipt})

        with patch.object(DiagnosticSession, "_owner", owner_waits_for_stop):
            session = DiagnosticSession(None, None, None, None)
            with patch.object(DiagnosticSession, "_wait_for_stop",
                              side_effect=TimeoutError("injected join timeout")):
                with self.assertRaisesRegex(TimeoutError, "injected join timeout"):
                    session.request("stop")
            release_cleanup.set()
            retried = session.request("stop")

        self.assertEqual(retried, receipt)
        self.assertTrue(session._closed)
        self.assertFalse(session._thread.is_alive())
        self.assertTrue(session._commands.empty())

    def test_private_probe_has_no_input_desktop_switch_path_or_automatic_consumer_launch(self):
        source = (ROOT / "modules" / "private_desktop" / "module.py").read_text(encoding="utf-8")
        self.assertNotIn("SwitchDesktop", source)
        self.assertIn("JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE", source)
        self.assertIn("EnumDesktopWindows", source)
        self.assertIn("PrintWindow", source)
        self.assertIn("CREATE_SUSPENDED", source)
        self.assertIn("--private-desktop-probe", source)
        self.assertIn("--private-desktop-uia-worker", source)
        self.assertIn("Invoke test action", (ROOT / "app.py").read_text(encoding="utf-8"))
        self.assertNotIn("Only Chat.exe", source)
        self.assertNotIn("ChatGPT.exe", source)

    def test_cleanup_close_failure_is_reported_without_finalizer_exception(self):
        cleanup = {"desktop_handle_closed": False}
        report = {"cleanup_error": "CloseDesktop: access denied"}
        _verify_desktop_removal(cleanup, Mock(), "AIVDDQA_test", report)
        self.assertIs(cleanup["desktop_removed"], False)
        self.assertEqual(report["cleanup_error"], "CloseDesktop: access denied")

    def test_cleanup_removal_readback_is_only_claimed_after_handle_close(self):
        cleanup = {"desktop_handle_closed": True}
        report = {}
        api = Mock()
        api.desktop_exists.return_value = False
        _verify_desktop_removal(cleanup, api, "AIVDDQA_test", report)
        self.assertIs(cleanup["desktop_removed"], True)
        api.desktop_exists.assert_called_once_with("AIVDDQA_test")

    def test_cleanup_removal_readback_retries_until_delayed_desktop_disappears(self):
        cleanup = {"desktop_handle_closed": True}
        report = {}
        api = Mock()
        api.desktop_exists.side_effect = [True, True, False]
        with patch("modules.private_desktop.module.time.sleep"):
            _verify_desktop_removal(cleanup, api, "AIVDDQA_test", report)
        self.assertIs(cleanup["desktop_removed"], True)
        self.assertEqual(api.desktop_exists.call_count, 3)

    def test_resources_not_created_before_input_desktop_denial_are_vacuously_closed(self):
        cleanup = {"process_stopped": False, "uia_worker_stopped": False,
                   "job_handle_closed": False, "desktop_handle_closed": False}
        absent = type("Absent", (), {"hProcess": None})()
        _mark_uncreated_probe_resources(cleanup, absent, absent, None, None)
        self.assertTrue(cleanup["process_stopped"])
        self.assertTrue(cleanup["uia_worker_stopped"])
        self.assertTrue(cleanup["job_handle_closed"])
        self.assertTrue(cleanup["desktop_handle_closed"])

    def test_packager_declares_runtime_only_stdlib_dependency(self):
        spec = (ROOT / "packaging" / "AI Virtual Display Manager.spec").read_text(encoding="utf-8")
        self.assertIn('"uuid"', spec)
        self.assertIn('"core.png_capture"', spec)
        self.assertIn('"core.win32_dpi"', spec)
        self.assertIn('("pywinauto", "comtypes")', spec)
        self.assertIn('"modules.private_desktop.session"', spec)
        self.assertIn("collect_all(package)", spec)
        self.assertIn('pywinauto==0.6.9', (ROOT / "requirements-build.txt").read_text())

    def test_bgra_png_preserves_dimensions_and_rgb_pixel_order(self):
        # Two top-down pixels: BGRA blue, then BGRA red.
        png = encode_bgra_png(bytes((255, 0, 0, 0, 0, 0, 255, 0)), 2, 1)
        self.assertTrue(png.startswith(PNG_SIGNATURE))
        offset = len(PNG_SIGNATURE)
        chunks = {}
        while offset < len(png):
            size = struct.unpack_from(">I", png, offset)[0]
            kind = png[offset + 4:offset + 8]
            data = png[offset + 8:offset + 8 + size]
            crc = struct.unpack_from(">I", png, offset + 8 + size)[0]
            self.assertEqual(crc, zlib.crc32(kind + data) & 0xFFFFFFFF)
            chunks.setdefault(kind, []).append(data)
            offset += 12 + size
        self.assertEqual(struct.unpack(">II", chunks[b"IHDR"][0][:8]), (2, 1))
        self.assertEqual(zlib.decompress(chunks[b"IDAT"][0]),
                         bytes((0, 0, 0, 255, 255, 255, 0, 0, 255)))
        self.assertEqual(chunks[b"IEND"], [b""])
        self.assertEqual(validate_encoded_png(png), (2, 1))

    def test_png_validator_rejects_signature_only_corrupt_chunks_and_oversize_pixels(self):
        valid = encode_bgra_png(bytes((1, 2, 3, 255)) * 4, 2, 2)
        corrupt_crc = valid[:-5] + bytes((valid[-5] ^ 1,)) + valid[-4:]
        for png in (PNG_SIGNATURE, corrupt_crc):
            with self.subTest(size=len(png)), self.assertRaises(ValueError):
                validate_encoded_png(png)

    def test_bgra_png_rejects_bad_dimensions_buffers_and_oversize(self):
        cases = [
            (b"", 0, 1),
            (b"", 1, 1),
            (bytes(MAX_CAPTURE_PIXELS * 4), MAX_CAPTURE_PIXELS + 1, 1),
        ]
        for pixels, width, height in cases:
            with self.subTest(width=width, height=height), self.assertRaises(ValueError):
                encode_bgra_png(pixels, width, height)
        self.assertLessEqual(MAX_PNG_BYTES, 4 * 1024 * 1024)

    def test_png_content_validation_uses_the_encoded_bgra_pixels(self):
        colored = bytes((0, 0, 0, 0, 0, 0, 255, 0,
                         0, 255, 0, 0, 255, 0, 0, 0))
        self.assertGreaterEqual(sampled_bgra_color_count(colored, 2, 2), 3)
        self.assertEqual(sampled_bgra_color_count(bytes((0, 0, 0, 255)) * 4, 2, 2), 1)
        self.assertEqual(sampled_bgra_color_count(bytes((255, 255, 255, 255)) * 4, 2, 2), 0)
        with self.assertRaises(ValueError):
            sampled_bgra_color_count(b"", 2, 2)

    def test_capture_boundary_rejects_blank_and_near_uniform_frames(self):
        colored = bytes((0, 0, 0, 0, 0, 0, 255, 0,
                         0, 255, 0, 0, 255, 0, 0, 0))
        self.assertGreaterEqual(require_contentful_bgra_capture(colored, 2, 2), 3)
        for pixels in (bytes((0, 0, 0, 255)) * 4,
                       bytes((255, 255, 255, 255)) * 4,
                       bytes((30, 30, 30, 255)) * 4 + bytes((31, 31, 31, 255)) * 4):
            width = 2
            height = len(pixels) // (width * 4)
            with self.subTest(colors=sampled_bgra_color_count(pixels, width, height)):
                with self.assertRaisesRegex(ValueError, "CAPTURE_CONTENT_UNVERIFIED"):
                    require_contentful_bgra_capture(pixels, width, height)


class ReadOnlyInputDesktopFallbackTests(unittest.TestCase):
    @staticmethod
    def make_api(foreground_name="Default", manager_name="Default", windows=()):
        from ctypes import wintypes

        api = object.__new__(_Win32)
        api.CALLBACK = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                          wintypes.LPARAM)
        api.kernel32 = types.SimpleNamespace(GetCurrentThreadId=lambda: 1)
        api.closed_handles = []
        api.kernel32.CloseHandle = lambda handle: api.closed_handles.append(handle) or True

        def denied_open(*_args):
            ctypes.set_last_error(5)
            return None

        def get_thread_desktop(thread_id):
            thread_id = int(thread_id)
            return 77 if thread_id == 1 else 88 if thread_id == 2 else 99

        def get_window_thread(_hwnd, pid_ptr):
            ctypes.cast(pid_ptr, ctypes.POINTER(wintypes.DWORD))[0] = 42
            return 2

        def enum_windows(desktop, callback, param):
            assert desktop == 77
            for hwnd in windows:
                callback(hwnd, param)
            return True

        api.user32 = types.SimpleNamespace(
            OpenInputDesktop=denied_open,
            GetForegroundWindow=lambda: 500,
            GetWindowThreadProcessId=get_window_thread,
            GetThreadDesktop=get_thread_desktop,
            EnumDesktopWindows=enum_windows,
        )
        api.object_name = lambda handle: (
            manager_name if handle == 77 else
            foreground_name if handle == 88 else "AIVDDS_PRIVATE")
        return api

    def test_access_denied_uses_matching_foreground_and_manager_desktop_names(self):
        api = self.make_api()
        self.assertEqual(api.input_desktop_name(), "Default")
        self.assertFalse(api.window_is_on_input_desktop(123))
        self.assertEqual(api.closed_handles, [])

    def test_access_denied_with_different_foreground_desktop_fails_closed(self):
        api = self.make_api(foreground_name="Winlogon")
        self.assertEqual(api.input_desktop_name(), "unknown")
        with self.assertRaisesRegex(OSError, "differs from the manager"):
            api.window_is_on_input_desktop(123)

    def test_access_denied_with_no_foreground_hwnd_is_unknown(self):
        api = self.make_api()
        api.user32.GetForegroundWindow = lambda: 0
        with self.assertRaisesRegex(OSError, "identity is unavailable"):
            api.input_desktop_name()

    def test_access_denied_with_distinct_manager_desktop_is_unknown(self):
        api = self.make_api(manager_name="AIVDDS_private")
        self.assertEqual(api.input_desktop_name(), "unknown")

    def test_stable_host_thread_survives_create_desktop_worker_reassignment(self):
        api = self.make_api()
        # Model the risky case where the probe worker is assigned to the new
        # desktop while foreground remains on the host desktop. Native QA in
        # this environment did not reproduce reassignment; this is a defensive
        # regression, not an assertion about every Windows session.
        api.kernel32.GetCurrentThreadId = lambda: 9
        self.assertEqual(api.input_desktop_name(), "unknown")
        self.assertEqual(api.input_desktop_name(manager_thread_id=1), "Default")
        self.assertFalse(api.window_is_on_input_desktop(123, manager_thread_id=1))
        with self.assertRaisesRegex(OSError, "differs from the manager"):
            api.window_is_on_input_desktop(123)

    @unittest.skipUnless(os.name == "nt", "Windows desktop APIs are required")
    def test_native_create_desktop_keeps_host_thread_identity_stable(self):
        api = _Win32()
        u, k = api.user32, api.kernel32
        host_thread_id = int(k.GetCurrentThreadId())
        host_desktop = u.GetThreadDesktop(host_thread_id)
        host_before = api.object_name(host_desktop)
        desktop_name = f"AIVDDS_THREAD_TEST_{uuid.uuid4().hex[:12]}"
        result, errors = {}, []

        def worker():
            worker_thread_id = int(k.GetCurrentThreadId())
            desktop = None
            try:
                desktop = u.CreateDesktopW(desktop_name, None, None, 0,
                                           0x00C3, None)
                if not desktop:
                    errors.append(ctypes.get_last_error())
                    return
                result["returned_desktop_name"] = api.object_name(desktop)
                result["worker_thread_id"] = worker_thread_id
                result["worker_desktop_after_create"] = api.object_name(
                    u.GetThreadDesktop(worker_thread_id))
                result["host_desktop_during_create"] = api.object_name(
                    u.GetThreadDesktop(host_thread_id))
                result["worker_restored"] = bool(u.SetThreadDesktop(host_desktop))
            finally:
                if desktop:
                    result["desktop_closed"] = bool(u.CloseDesktop(desktop))

        thread = threading.Thread(target=worker, name="desktop-reassignment-test")
        thread.start()
        thread.join(timeout=4)
        self.assertFalse(thread.is_alive(), "native desktop test worker did not exit")
        if errors:
            self.skipTest(f"CreateDesktopW unavailable in this session: Win32 {errors[0]}")
        self.assertTrue(result.get("desktop_closed"))
        self.assertTrue(result.get("worker_restored"))
        self.assertNotEqual(result.get("worker_thread_id"), host_thread_id)
        self.assertEqual(result.get("returned_desktop_name"), desktop_name)
        self.assertIn(result.get("worker_desktop_after_create"),
                      {host_before, desktop_name},
                      msg=f"native desktop thread evidence: {result!r}; host={host_before!r}")
        self.assertEqual(result.get("host_desktop_during_create"), host_before)
        self.assertEqual(api.object_name(u.GetThreadDesktop(host_thread_id)), host_before)
        self.assertFalse(api.desktop_exists(desktop_name))


if __name__ == "__main__":
    unittest.main(verbosity=2)
