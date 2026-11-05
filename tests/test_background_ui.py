import sys
import unittest
from queue import Empty
from unittest.mock import Mock, patch

from core.background_ui import BackgroundUI
from app import ManagerApp, main
from modules.manager_cli.module import ManagerCliModule, REQUEST_COMMANDS
from tools.vdmctl import build_parser


class BackgroundUITests(unittest.TestCase):
    def test_mailbox_is_bounded_and_receipt_is_not_applied_state(self):
        ui = BackgroundUI()
        receipt = ui.request("show")
        self.assertTrue(receipt["accepted"])
        self.assertFalse(receipt["visible"])
        with self.assertRaisesRegex(RuntimeError, "UI_REQUEST_PENDING"):
            ui.request("hide")
        self.assertEqual(ui.take(), "show")
        self.assertIsNone(ui.take())
        ui.publish(True)
        self.assertTrue(ui.request("status")["visible"])
        with self.assertRaises(ValueError):
            ui.request("shell")

    def test_hide_preserves_engine_and_show_refreshes_on_demand(self):
        app = ManagerApp.__new__(ManagerApp)
        app.root = Mock()
        app._background_ui = BackgroundUI()
        app.refresh = Mock()
        app.hide_window()
        app.root.withdraw.assert_called_once()
        app.root.destroy.assert_not_called()
        app.show_window()
        app.root.deiconify.assert_called_once()
        app.refresh.assert_called_once()

    def test_stop_request_schedules_guarded_async_shutdown(self):
        app = ManagerApp.__new__(ManagerApp)
        app._background_ui = BackgroundUI()
        app._busy = False
        app._closing = False
        app._completed = Mock()
        app._completed.get_nowait.side_effect = Empty
        app._task_runtime_action = Mock(return_value={"active_session_id": "active"})
        app.close = Mock()
        app.root = Mock()
        app.root.state.return_value = "withdrawn"
        app._background_ui.request("stop")
        app._drain_completions()
        app._task_runtime_action.assert_not_called()
        app.close.assert_called_once_with(verify_no_active_task=True)
        state = app._background_ui.request("status")
        self.assertIsNone(state["last_error"])
        app.root.destroy.assert_not_called()

    def test_default_main_withdraws_without_iconify_or_dialog(self):
        root, guard = Mock(), Mock()
        with patch.object(sys, "argv", ["app.py"]), \
                patch("app.SingleInstanceGuard.acquire", return_value=guard), \
                patch("app.tk.Tk", return_value=root), \
                patch("app.ManagerApp") as app, \
                patch("app.messagebox.showerror") as error:
            main()
        root.withdraw.assert_called_once()
        root.iconify.assert_not_called()
        app.return_value.show_window.assert_not_called()
        error.assert_not_called()
        guard.close.assert_called_once()

    def test_duplicate_default_launch_is_silent_and_creates_no_tk(self):
        with patch.object(sys, "argv", ["app.py"]), \
                patch("app.SingleInstanceGuard.acquire", return_value=None), \
                patch("app.tk.Tk") as tk:
            main()
        tk.assert_not_called()

    def test_explicit_duplicate_show_routes_to_cli_without_second_window(self):
        exe = r"C:\Python313\pythonw.exe"
        with patch.object(sys, "argv", ["app.py", "--show-ui"]), \
                patch("app.sys.executable", exe), \
                patch("app.SingleInstanceGuard.acquire", return_value=None), \
                patch("app.Path.is_file", return_value=True), \
                patch("app.subprocess.Popen") as launch, \
                patch("app.tk.Tk") as tk:
            main()
        launch.assert_called_once()
        command = launch.call_args.args[0]
        self.assertEqual(command[0], r"C:\Python313\python.exe")
        self.assertEqual(command[-2:], ["ui", "show"])
        tk.assert_not_called()

    def test_default_startup_failure_logs_without_a_dialog(self):
        with patch.object(sys, "argv", ["app.py"]), \
                patch("app.SingleInstanceGuard.acquire", return_value=Mock()), \
                patch("app.tk.Tk", return_value=Mock()), \
                patch("app.ManagerApp", side_effect=RuntimeError("fixture failure")), \
                patch("app.atomic_json") as record, \
                patch("app.messagebox.showerror") as dialog:
            main()
        dialog.assert_not_called()
        self.assertIn("fixture failure", record.call_args.args[1]["error"])

    def test_cli_ui_commands_are_fixed_and_require_no_arguments(self):
        module = ManagerCliModule.__new__(ManagerCliModule)
        ui = BackgroundUI()
        module._ui_action = ui.request
        for action in ("show", "hide", "stop", "status"):
            self.assertIn("ui." + action, REQUEST_COMMANDS)
            parsed = build_parser().parse_args(["ui", action])
            self.assertEqual((parsed.group, parsed.action), ("ui", action))
        self.assertTrue(module._dispatch_command("ui.show", {})["accepted"])
        with self.assertRaises(ValueError):
            module._dispatch_command("ui.hide", {"arbitrary": True})
        module._ui_action = None
        with self.assertRaisesRegex(ValueError, "UI_CONTROL_UNAVAILABLE"):
            module._dispatch_command("ui.status", {})


if __name__ == "__main__":
    unittest.main()
