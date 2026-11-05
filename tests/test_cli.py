from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app


class HiddenCliDispatchTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "requires a local Windows named pipe")
    def test_run_self_check_composes_and_stops_a_disposable_task_pipe(self):
        caller_argv = list(sys.argv)
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "self-check.json"
            result = app.run_self_check(str(report))
            data = json.loads(report.read_text(encoding="utf-8"))
            if result != 0:
                self.fail(json.dumps({key: data.get(key) for key in (
                    "error", "cleanup_error", "task_runtime_composition")},
                    ensure_ascii=False))
            composition = data["task_runtime_composition"]
            self.assertTrue(composition["passed"])
            self.assertTrue(composition["checks"]["runtime_bound"])
            self.assertTrue(composition["checks"]["output_lease_bound"])
            self.assertTrue(composition["checks"]["backend_unavailable"])
            self.assertTrue(composition["checks"]["zero_targets"])
            self.assertTrue(composition["checks"]["lease_fail_closed"])
            self.assertTrue(composition["disposable_pipe_stopped"])
            self.assertNotEqual(composition["pipe_path"],
                                r"\\.\pipe\OnlyChatAiTaskRuntime.v2")
            self.assertFalse(data["modules"]["only_chat_debug_display"]["state"]["connected"])
            self.assertFalse(data["modules"]["task_display_lease"]["state"]["pipe_ready"])
            self.assertEqual(
                data["modules"]["task_display_lease"]["state"]["error_code"],
                "BROKER_NOT_STARTED_IN_SELF_CHECK",
            )
            self.assertFalse(data["modules"]["only_chat_output_lease"]["state"]["pipe_ready"])
            self.assertEqual(
                data["modules"]["only_chat_output_lease"]["state"]["error_code"],
                "BROKER_NOT_STARTED_IN_SELF_CHECK",
            )
        self.assertEqual(sys.argv, caller_argv)

    def test_malformed_reserved_check_flag_returns_error_without_opening_ui(self):
        for argv in (["--self-check"], ["--self-check", ""],
                     ["--private-desktop-session-self-check", "report.json", "extra"],
                     ["--task-runtime-private-desktop-self-check", "report.json", "extra"],
                     ["--private-desktop-uia-worker", "hwnd"],
                     ["--private-desktop-uia-action-worker", "hwnd"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()) as stderr:
                with patch.object(app, "main") as open_ui:
                    self.assertEqual(app.dispatch_hidden_cli(list(argv)), 2)
                    open_ui.assert_not_called()
                self.assertIn("Invalid arguments", stderr.getvalue())

    def test_malformed_reserved_check_with_missing_stderr_still_fails_closed(self):
        with patch.object(app.sys, "stderr", None), patch.object(app, "main") as open_ui:
            self.assertEqual(app.dispatch_hidden_cli(["--self-check"]), 2)
            open_ui.assert_not_called()

    def test_valid_hidden_check_dispatches_only_to_requested_handler(self):
        handler = Mock(return_value=7)
        with patch.object(app, "run_self_check", handler):
            self.assertEqual(app.dispatch_hidden_cli(["--self-check", "report.json"]), 7)
        handler.assert_called_once_with("report.json")

    def test_task_runtime_diagnostic_flag_dispatches_only_to_owned_target_check(self):
        handler = Mock(return_value=9)
        with patch.object(app, "run_task_runtime_private_desktop_self_check", handler):
            self.assertEqual(app.dispatch_hidden_cli([
                "--task-runtime-private-desktop-self-check", "report.json"]), 9)
        handler.assert_called_once_with("report.json")

    def test_hidden_handler_exception_becomes_bounded_report_without_escaping(self):
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "failure.json"
            handler = Mock(side_effect=RuntimeError("diagnostic fault"))
            with patch.object(app, "run_self_check", handler), \
                    patch.object(app.sys, "stderr", None):
                self.assertEqual(app.dispatch_hidden_cli(["--self-check", str(report)]), 1)
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertFalse(data["passed"])
            self.assertEqual(data["error"], "RuntimeError: diagnostic fault")

    def test_hidden_handler_exception_preserves_an_existing_detailed_report(self):
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "failure.json"
            report.write_text('{"detail":"keep"}', encoding="utf-8")
            handler = Mock(side_effect=RuntimeError("late fault"))
            with patch.object(app, "run_self_check", handler), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(app.dispatch_hidden_cli(["--self-check", str(report)]), 1)
            self.assertEqual(json.loads(report.read_text(encoding="utf-8")), {"detail": "keep"})

    def test_non_reserved_arguments_leave_normal_startup_available(self):
        self.assertIsNone(app.dispatch_hidden_cli([]))
        self.assertIsNone(app.dispatch_hidden_cli(["--user-mode"]))

    def test_startup_error_does_not_raise_secondary_destroyed_root_error(self):
        class DestroyedRoot:
            def withdraw(self):
                pass

            def destroy(self):
                raise app.tk.TclError('can\'t invoke "destroy": application has been destroyed')

        guard = Mock()
        with patch.object(app.sys, "argv", ["app.py", "--show-ui"]), \
                patch.object(app.SingleInstanceGuard, "acquire", return_value=guard), \
                patch.object(app.tk, "Tk", return_value=DestroyedRoot()), \
                patch.object(app, "ManagerApp", side_effect=RuntimeError("startup fault")), \
                patch.object(app.messagebox, "showerror") as showerror:
            app.main()
        showerror.assert_called_once()
        self.assertIn("startup fault", showerror.call_args.args[1])
        guard.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main(verbosity=2)
