from __future__ import annotations

import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.module_host import ModuleHost
from core.paths import APP_VERSION
from core.vdd_pipe import validate_command
from modules.mtt_vdd.module import MttVddModule
from app import manager_window_geometry


class PipeCommandTests(unittest.TestCase):
    def test_allowlist_and_range(self):
        self.assertEqual(validate_command("ping"), "PING")
        self.assertEqual(validate_command("SETDISPLAYCOUNT 0"), "SETDISPLAYCOUNT 0")
        self.assertEqual(validate_command("setdisplaycount 8"), "SETDISPLAYCOUNT 8")

    def test_rejects_arbitrary_or_unsafe_commands(self):
        for value in ("RELOAD_DRIVER", "SETGPU GPU", "SETDISPLAYCOUNT 9", "SETDISPLAYCOUNT -1", "PING ; whoami"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_command(value)

    def test_device_action_waits_for_elevated_result_and_verifies_status(self):
        module = MttVddModule()
        module.restore({"device": {"installed": True, "manufacturer": "MikeTheTech",
                                    "instance_id": r"ROOT\DISPLAY\0000", "status": "Error",
                                    "problem_code": 22}, "pipe": {"connected": False}})
        refresh_count = 0
        def refresh(**_kwargs):
            nonlocal refresh_count
            refresh_count += 1
            if refresh_count > 1:
                module._last["device"].update(status="Started", problem_code=None)
                module._last["pipe"] = {"connected": True}
            return module.snapshot()
        module.refresh = refresh
        def shell_execute(pointer):
            request = pointer._obj
            self.assertEqual(request.lpVerb, "runas")
            self.assertEqual(request.lpFile.lower().endswith(r"system32\windowspowershell\v1.0\powershell.exe"), True)
            self.assertIn("-NoProfile -NonInteractive -File", request.lpParameters)
            self.assertIn("enable_device.ps1", request.lpParameters)
            self.assertIn('-InstanceId "ROOT\\DISPLAY\\0000"', request.lpParameters)
            self.assertNotIn("ExecutionPolicy", request.lpParameters)
            request.hProcess = 123
            return 1
        shell = SimpleNamespace(ShellExecuteExW=Mock(side_effect=shell_execute))
        kernel = SimpleNamespace(WaitForSingleObject=Mock(return_value=0),
                                 GetExitCodeProcess=Mock(side_effect=lambda handle, code: (setattr(code._obj, "value", 0) or 1)),
                                 CloseHandle=Mock(return_value=1))
        with patch("modules.mtt_vdd.module.ctypes.WinDLL", side_effect=lambda name, **_: shell if name == "shell32" else kernel):
            result = module.device_action("enable")
        self.assertIn("종료 코드 0", result)
        self.assertIn("제어 파이프는 연결됨", result)
        kernel.WaitForSingleObject.assert_called_once()
        kernel.CloseHandle.assert_called_once_with(123)
        with self.assertRaises(ValueError):
            module.device_action("install")

    def test_device_action_reports_uac_cancellation_without_claiming_success(self):
        module = MttVddModule()
        module.restore({"device": {"installed": True, "manufacturer": "MikeTheTech",
                                    "instance_id": r"ROOT\DISPLAY\0000"}})
        module.refresh = lambda **_kwargs: module.snapshot()
        shell = SimpleNamespace(ShellExecuteExW=Mock(return_value=0))
        kernel = SimpleNamespace(WaitForSingleObject=Mock(return_value=0),
                                 GetExitCodeProcess=Mock(return_value=1), CloseHandle=Mock(return_value=1))
        with patch("modules.mtt_vdd.module.ctypes.WinDLL", side_effect=lambda name, **_: shell if name == "shell32" else kernel), \
             patch("modules.mtt_vdd.module.ctypes.get_last_error", return_value=1223):
            with self.assertRaisesRegex(RuntimeError, "취소"):
                module.device_action("enable")

    def test_device_action_does_not_claim_success_when_code_22_remains(self):
        module = MttVddModule()
        module.restore({"device": {"installed": True, "manufacturer": "MikeTheTech",
                                    "instance_id": r"ROOT\DISPLAY\0000", "status": "Error",
                                    "problem_code": 22}})
        def refresh(**_kwargs):
            return module.snapshot()
        module.refresh = refresh
        shell = SimpleNamespace(ShellExecuteExW=Mock(side_effect=lambda pointer: (setattr(pointer._obj, "hProcess", 123) or 1)))
        kernel = SimpleNamespace(WaitForSingleObject=Mock(return_value=0),
                                 GetExitCodeProcess=Mock(side_effect=lambda handle, code: (setattr(code._obj, "value", 0) or 1)),
                                 CloseHandle=Mock(return_value=1))
        with patch("modules.mtt_vdd.module.ctypes.WinDLL", side_effect=lambda name, **_: shell if name == "shell32" else kernel):
            with self.assertRaisesRegex(RuntimeError, "활성으로 확인하지 못했습니다"):
                module.device_action("enable")

    def test_enabled_device_is_a_noop_without_requesting_uac(self):
        module = MttVddModule()
        module.restore({"device": {"installed": True, "manufacturer": "MikeTheTech",
                                    "instance_id": r"ROOT\DISPLAY\0000", "status": "OK",
                                    "problem_code": None}})
        module.refresh = lambda **_kwargs: module.snapshot()
        with patch("modules.mtt_vdd.module.ctypes.WinDLL") as load_dll:
            result = module.device_action("enable")
        self.assertIn("이미 활성", result)
        load_dll.assert_not_called()

    def test_enable_helper_is_exact_device_guarded_and_does_not_bypass_policy(self):
        script = (ROOT / "modules" / "mtt_vdd" / "enable_device.ps1").read_text(encoding="utf-8")
        self.assertIn("Enable-PnpDevice -InstanceId $InstanceId -Confirm:$false -ErrorAction Stop", script)
        self.assertIn("$device.Class -ne 'Display'", script)
        self.assertIn("$device.Manufacturer -ne 'MikeTheTech'", script)
        self.assertIn("$device.FriendlyName -ne 'Virtual Display Driver'", script)
        self.assertNotIn("ExecutionPolicy", script)
        self.assertNotIn("pnputil", script.casefold())


class ModuleHostTests(unittest.TestCase):
    def test_manager_window_geometry_fits_compact_virtual_display(self):
        self.assertEqual(manager_window_geometry(800, 600), (768, 552, 16, 16))
        width, height, x, y = manager_window_geometry(2560, 1440)
        self.assertEqual((width, height), (980, 760))
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)

    def setUp(self):
        self.host = ModuleHost(ROOT)
        self.host.load("mtt_vdd")

    def tearDown(self):
        self.host.dispose_all()

    def test_isolated_reload_preserves_snapshot_and_reports_hash_generation(self):
        item = self.host._loaded["mtt_vdd"].instance
        item.restore({"remembered": {"target": "qa"}})
        old = self.host.info("mtt_vdd")
        new = self.host.reload("mtt_vdd")
        self.assertEqual(self.host.snapshot("mtt_vdd"), {"remembered": {"target": "qa"}})
        self.assertEqual(new["generation"], old["generation"] + 1)
        self.assertEqual(len(new["sha256"]), 64)

    def test_reload_is_blocked_during_driver_operation(self):
        self.host.active_operations = 1
        try:
            with self.assertRaisesRegex(RuntimeError, "active driver operation"):
                self.host.reload("mtt_vdd")
        finally:
            self.host.active_operations = 0

    def test_invoke_requires_a_module_exported_action_and_releases_operation_guard(self):
        with self.assertRaisesRegex(ValueError, "not exported"):
            self.host.invoke("mtt_vdd", "enable")
        self.assertEqual(self.host.active_operations, 0)

        self.host._loaded["test_actions"] = SimpleNamespace(
            module=SimpleNamespace(ACTIONS=("echo",), __name__="test_actions"),
            instance=SimpleNamespace(action=lambda name, payload: (name, payload),
                                     dispose=lambda: None),
        )
        self.assertEqual(self.host.invoke("test_actions", "echo", {"id": "qa"}),
                         ("echo", {"id": "qa"}))
        self.assertEqual(self.host.active_operations, 0)

    def test_module_reload_is_blocked_during_exported_action(self):
        self.host._loaded["test_actions"] = SimpleNamespace(
            module=SimpleNamespace(ACTIONS=("reload",), __name__="test_actions"),
            instance=SimpleNamespace(action=lambda _name, _payload:
                                     self.host.reload("mtt_vdd"), dispose=lambda: None),
        )
        with self.assertRaisesRegex(RuntimeError, "active driver operation"):
            self.host.invoke("test_actions", "reload")
        self.assertEqual(self.host.active_operations, 0)

    def test_noncanonical_module_path_is_rejected(self):
        other = ModuleHost(ROOT)
        with self.assertRaisesRegex(ValueError, "canonical path"):
            other.load("mtt_vdd", Path(__file__).resolve())

    def test_module_registry_is_explicit_and_versioned(self):
        manifest = json.loads((ROOT / "module-registry.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["appVersion"], APP_VERSION)
        spec = next(m for m in manifest["modules"] if m["id"] == "mtt_vdd")
        self.assertEqual(spec["apiVersion"], 1)
        self.assertEqual(spec["version"], self.host.info("mtt_vdd")["version"])
        topology_spec = next(m for m in manifest["modules"] if m["id"] == "display_topology")
        self.assertEqual(topology_spec["apiVersion"], 1)

    def test_topology_feature_is_separately_reloadable(self):
        self.host.load("display_topology")
        try:
            self.host._loaded["display_topology"].instance.restore({
                "displays": [{"name": "synthetic"}], "topology_verified": True, "error": ""
            })
            before = self.host.info("display_topology")
            after = self.host.reload("display_topology")
            self.assertEqual(after["generation"], before["generation"] + 1)
            self.assertEqual(self.host.snapshot("display_topology")["displays"], [{"name": "synthetic"}])
        finally:
            self.host.dispose_all()

    def test_app_window_router_load_is_metadata_only_and_reloadable(self):
        self.host.load("display_topology")
        self.host.load("app_window_router")
        before = self.host.info("app_window_router")
        state = self.host.refresh("app_window_router")
        self.assertEqual(state["window_enumeration"], "on_explicit_dialog_open_only")
        after = self.host.reload("app_window_router")
        self.assertEqual(after["generation"], before["generation"] + 1)
        self.assertEqual(after["version"], before["version"])

    def test_failed_candidate_init_rolls_back_to_previous_module(self):
        path = self.host._loaded["mtt_vdd"].path
        original = path.read_bytes()
        old = self.host.info("mtt_vdd")
        broken = '''API_VERSION=1\nMODULE_ID="mtt_vdd"\nVERSION=__TEST_VERSION__\nclass Broken:\n def restore(self, state): pass\n def init(self): raise RuntimeError("candidate rejected")\n def dispose(self): pass\n def snapshot(self): return {}\n def command(self, command): return ""\ndef create(): return Broken()\n'''.replace('__TEST_VERSION__', repr(old['version']))
        try:
            path.write_text(broken, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "candidate rejected"):
                self.host.reload("mtt_vdd")
            self.assertEqual(self.host.info("mtt_vdd"), old)
            self.assertEqual(self.host.snapshot("mtt_vdd"), {"device": {}, "pipe": {}, "monitors": []})
        finally:
            path.write_bytes(original)


if __name__ == "__main__":
    unittest.main(verbosity=2)





