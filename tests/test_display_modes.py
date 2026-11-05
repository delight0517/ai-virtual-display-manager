from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.display_topology.module import DisplayTopologyModule


class DisplayModeTests(unittest.TestCase):
    def setUp(self):
        self.module = DisplayTopologyModule()
        self.path = r"\\?\DISPLAY#MTT#VIRTUAL0"
        self.device = r"DISPLAY\MTT\0000"
        self.mtt = r"ROOT\DISPLAY\0000"
        self.row = {
            "display_name": r"\\.\DISPLAY10",
            "monitor_device_id": self.device,
            "monitor_path": self.path,
            "topology_verified": True,
            "cloned": False,
            "width": 800,
            "height": 600,
            "refresh_rate": 60,
            "available_modes": [(800, 600, 60), (1920, 1080, 60), (2560, 1440, 60)],
        }

    def target(self, row=None, *, parent=None, verified=True, mode=(2560, 1440, 60)):
        return self.module._mode_target(
            [row or self.row], display_name=self.row["display_name"],
            monitor_device_id=self.device, monitor_path=self.path,
            monitor_parent=parent or self.mtt, mtt_instance_id=self.mtt,
            mode=mode, topology_verified=verified,
        )

    def test_mode_target_requires_exact_uncloned_verified_endpoint_and_mtt_parent(self):
        row, mode = self.target()
        self.assertIs(row, self.row)
        self.assertEqual(mode, (2560, 1440, 60))
        stale = dict(self.row, monitor_path="different")
        with self.assertRaisesRegex(RuntimeError, "changed or is cloned"):
            self.target(row=stale)
        with self.assertRaisesRegex(PermissionError, "not a child"):
            self.target(parent=r"DISPLAY\PHYSICAL\0001")
        with self.assertRaisesRegex(RuntimeError, "not verified"):
            self.target(verified=False)

    def test_mode_target_rejects_unsupported_mode_and_clone(self):
        with self.assertRaisesRegex(ValueError, "not currently supported"):
            self.target(mode=(3840, 2160, 60))
        with self.assertRaisesRegex(RuntimeError, "changed or is cloned"):
            self.target(row=dict(self.row, cloned=True))

    def test_unconfirmed_mode_change_does_not_query_or_mutate_display(self):
        self.module.refresh = Mock(side_effect=AssertionError("must not read/mutate"))
        with self.assertRaisesRegex(PermissionError, "explicit in-app confirmation"):
            self.module.apply_mtt_mode(
                display_name=self.row["display_name"], monitor_device_id=self.device,
                monitor_path=self.path, monitor_parent=self.mtt, mtt_instance_id=self.mtt,
                mode=(2560, 1440, 60), confirmed=False,
            )
        self.module.refresh.assert_not_called()

    def test_apply_mode_tests_then_applies_and_verifies_exact_endpoint(self):
        before = {"displays": [dict(self.row)], "topology_verified": True, "error": ""}
        after_row = dict(self.row, width=2560, height=1440)
        after = {"displays": [after_row], "topology_verified": True, "error": ""}
        self.module.refresh = Mock(side_effect=[before, after])
        api = Mock()
        api.ChangeDisplaySettingsExW = Mock(return_value=0)
        with patch("modules.display_topology.module.ctypes.WinDLL", return_value=api):
            result = self.module.apply_mtt_mode(
                display_name=self.row["display_name"], monitor_device_id=self.device,
                monitor_path=self.path, monitor_parent=self.mtt, mtt_instance_id=self.mtt,
                mode=(2560, 1440, 60), confirmed=True,
            )
        self.assertEqual(result["state"], "applied")
        self.assertEqual(api.ChangeDisplaySettingsExW.call_count, 2)
        self.assertEqual(result["previous_mode"], [800, 600, 60])
        self.assertEqual(result["mode"], [2560, 1440, 60])

    def test_apply_refuses_before_native_call_when_endpoint_is_not_exact(self):
        before = {"displays": [dict(self.row, cloned=True)], "topology_verified": True, "error": ""}
        self.module.refresh = Mock(return_value=before)
        api = Mock()
        with patch("modules.display_topology.module.ctypes.WinDLL", return_value=api):
            with self.assertRaisesRegex(RuntimeError, "changed or is cloned"):
                self.module.apply_mtt_mode(
                    display_name=self.row["display_name"], monitor_device_id=self.device,
                    monitor_path=self.path, monitor_parent=self.mtt, mtt_instance_id=self.mtt,
                    mode=(2560, 1440, 60), confirmed=True,
                )
        api.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
