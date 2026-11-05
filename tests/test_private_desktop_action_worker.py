from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from app import run_private_desktop_uia_action_worker


class _Control:
    def __init__(self, kind, name, *, state=0):
        self.element_info = types.SimpleNamespace(control_type=kind)
        self.name = name
        self.state = state
        self.invocations = 0

    def window_text(self):
        return self.name

    def invoke(self):
        self.invocations += 1
        self.name = "Opened"

    def get_toggle_state(self):
        return self.state

    def toggle(self):
        self.state = 0 if self.state == 1 else 1


class PrivateDesktopActionWorkerTests(unittest.TestCase):
    def run_worker(self, descriptor, controls):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        descriptor_path = Path(temp.name) / "action.json"
        report_path = Path(temp.name) / "report.json"
        descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")

        class Window:
            def wait(self, *_args, **_kwargs):
                return True

            def descendants(self):
                return controls

        class DesktopFactory:
            def __init__(self, **_kwargs):
                pass

            def window(self, **_kwargs):
                return Window()

        comtypes = types.ModuleType("comtypes")
        comtypes.CoInitialize = lambda: None
        comtypes.CoUninitialize = lambda: None
        pywinauto = types.ModuleType("pywinauto")
        pywinauto.Desktop = DesktopFactory
        with patch.dict(sys.modules, {"comtypes": comtypes, "pywinauto": pywinauto}):
            code = run_private_desktop_uia_action_worker(
                "33", str(descriptor_path), str(report_path))
        return code, json.loads(report_path.read_text(encoding="utf-8"))

    def test_exact_button_action_requires_the_named_postcondition(self):
        button = _Control("Button", "Open panel")
        code, report = self.run_worker({
            "control_type": "Button", "control_name": "Open panel",
            "postcondition_name": "Name", "postcondition_value": "Opened",
        }, [button])
        self.assertEqual(code, 0)
        self.assertEqual(button.invocations, 1)
        self.assertEqual(report["postcondition"], {"Name": "Opened"})

    def test_checkbox_toggles_only_to_exact_pinned_state(self):
        checkbox = _Control("CheckBox", "Use transparency", state=0)
        code, report = self.run_worker({
            "control_type": "CheckBox", "control_name": "Use transparency",
            "postcondition_name": "ToggleState", "postcondition_value": "On",
        }, [checkbox])
        self.assertEqual(code, 0)
        self.assertEqual(checkbox.state, 1)
        self.assertEqual(report["postcondition"], {"ToggleState": "On"})

    def test_ambiguous_control_name_fails_without_invoking(self):
        first, second = _Control("Button", "Open panel"), _Control("Button", "Open panel")
        code, report = self.run_worker({
            "control_type": "Button", "control_name": "Open panel",
            "postcondition_name": "Name", "postcondition_value": "Opened",
        }, [first, second])
        self.assertEqual(code, 1)
        self.assertFalse(report["performed"])
        self.assertEqual((first.invocations, second.invocations), (0, 0))

    def test_rejects_extra_descriptor_fields_before_uia(self):
        code, report = self.run_worker({
            "control_type": "Button", "control_name": "Open panel",
            "postcondition_name": "Name", "postcondition_value": "Opened",
            "command": "arbitrary",
        }, [_Control("Button", "Open panel")])
        self.assertEqual(code, 1)
        self.assertFalse(report["performed"])


if __name__ == "__main__":
    unittest.main()
