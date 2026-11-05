import unittest

from modules.mtt_vdd.module import classify_monitor_parent
from modules.app_window_router.physical_guard import correction_target, topology_ready
from modules.app_window_router.verified_cursor import _topology_snapshot


class MttVddOwnershipTests(unittest.TestCase):
    def test_stardesk_root_display_adapter_is_virtual(self):
        value, evidence = classify_monitor_parent(r"ROOT\DISPLAY\0001", {r"ROOT\DISPLAY\0000"})
        self.assertIs(value, True)
        self.assertEqual(evidence, "root_enumerated_display_adapter")

    def test_mtt_parent_is_virtual_with_exact_device_evidence(self):
        value, evidence = classify_monitor_parent(r"ROOT\DISPLAY\0000", {r"ROOT\DISPLAY\0000"})
        self.assertIs(value, True)
        self.assertEqual(evidence, "verified_mtt_adapter_parent")

    def test_other_root_enumerated_adapter_is_not_physical(self):
        value, evidence = classify_monitor_parent(r"ROOT\SDIddDriver\0001")
        self.assertIs(value, True)
        self.assertEqual(evidence, "root_enumerated_adapter")

    def test_non_root_monitor_parent_remains_physical(self):
        value, evidence = classify_monitor_parent(r"PCI\VEN_10DE&DEV_1B80\4&120963E7&0&0009")
        self.assertIs(value, False)
        self.assertEqual(evidence, "non_root_adapter_parent")

    def test_failed_parent_lookup_is_unknown_not_physical(self):
        value, evidence = classify_monitor_parent("")
        self.assertIsNone(value)
        self.assertEqual(evidence, "parent_unavailable")

    def test_stardesk_output_is_not_a_physical_window_recovery_target(self):
        mtt = r"ROOT\DISPLAY\0000"
        topology = {
            "topology_verified": True,
            "mtt_device_instance_id": mtt,
            "displays": [
                {"x": 0, "y": 0, "width": 1920, "height": 1080,
                 "virtual": False, "monitor_parent": r"PCI\VEN_10DE&DEV_1B80",
                 "attached": True, "verified": True, "cloned": False,
                 "topology_verified": True, "monitor_identity_verified": True},
                {"x": 1920, "y": 0, "width": 1920, "height": 1080,
                 "virtual": True, "monitor_parent": mtt,
                 "attached": True, "verified": True, "cloned": False,
                 "topology_verified": True, "monitor_identity_verified": True},
                {"x": 3840, "y": 0, "width": 1920, "height": 1080,
                 "virtual": True, "monitor_parent": r"ROOT\DISPLAY\0001",
                 "attached": True, "verified": True, "cloned": False,
                 "topology_verified": True, "monitor_identity_verified": True},
            ],
        }
        self.assertTrue(topology_ready(topology))
        window = {"bounds": [3900, 100, 4300, 500], "window_state": "normal",
                  "bounds_space": "screen", "class_name": "AppWindow"}
        self.assertIsNone(correction_target(window, topology))
        self.assertIsNone(_topology_snapshot(topology))


if __name__ == "__main__":
    unittest.main()
