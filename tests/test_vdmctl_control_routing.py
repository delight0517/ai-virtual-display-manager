import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from tools.vdmctl import (CONTROL_PIPE_PATH, PIPE_PATH, UI_CONTROL_PIPE_PATH,
                          build_parser, main, pipe_for_command)


class VdmctlControlRoutingTests(unittest.TestCase):
    def test_task_control_commands_use_task_lane(self):
        self.assertEqual(pipe_for_command("task.status"), CONTROL_PIPE_PATH)
        self.assertEqual(pipe_for_command("task.stop"), CONTROL_PIPE_PATH)

    def test_router_private_target_is_an_explicit_cli_choice_and_forwarded(self):
        args = build_parser().parse_args([
            "task", "start", "--target", "ai_account_router_qa", "--outputs", "0", "--confirm"
        ])
        self.assertEqual(args.target, "ai_account_router_qa")
        self.assertEqual(args.outputs, 0)
        output = StringIO()
        with patch("tools.vdmctl.send_request", return_value={"ok": True, "data": {}}) as send:
            with redirect_stdout(output):
                main(["task", "start", "--target", "ai_account_router_qa", "--outputs", "0", "--confirm"])
        command, payload = send.call_args.args[:2]
        self.assertEqual(command, "task.start")
        self.assertEqual(payload, {"confirmed": True, "output_count": 0,
                                   "profile": "private", "target_app_id": "ai_account_router_qa"})

    def test_task_target_choices_remain_closed(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["task", "start", "--target", "arbitrary", "--confirm"])

    def test_ui_commands_use_isolated_ui_lane(self):
        for command in ("ui.status", "ui.show", "ui.hide", "ui.stop"):
            with self.subTest(command=command):
                self.assertEqual(pipe_for_command(command), UI_CONTROL_PIPE_PATH)

    def test_general_commands_keep_main_lane(self):
        for command in ("status", "screens", "windows", "logs", "outputs.preview"):
            with self.subTest(command=command):
                self.assertEqual(pipe_for_command(command), PIPE_PATH)


if __name__ == "__main__":
    unittest.main()
