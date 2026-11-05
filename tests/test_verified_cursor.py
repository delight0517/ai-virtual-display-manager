import unittest

from modules.app_window_router.verified_cursor import VerifiedCursorClip


PHYSICAL = (0, 0, 2560, 1440)
DESKTOP = (-1920, 0, 2560, 1440)


def topology(*, virtual_x=-1920, verified=True, cloned=False, parent="mtt#0"):
    return {
        "topology_verified": verified,
        "mtt_device_instance_id": "mtt#0",
        "displays": [
            {"attached": True, "verified": True, "topology_verified": True,
             "monitor_identity_verified": True, "cloned": False, "clone_source_count": 1,
             "virtual": False,
             "monitor_parent": "physical#0", "monitor_path": "path-physical",
             "display_name": "DISPLAY1", "adapter": "Physical adapter",
             "adapter_device_id": "adapter-physical", "monitor_device_id": "monitor-physical",
             "target_name": "DELL P2421D",
             "x": 0, "y": 0, "width": 2560, "height": 1440},
            {"attached": True, "verified": True, "topology_verified": True,
             "monitor_identity_verified": True, "cloned": cloned, "clone_source_count": 1,
             "virtual": True,
             "monitor_parent": parent, "monitor_path": "path-virtual",
             "display_name": "DISPLAY32", "adapter": "MTT VDD",
             "adapter_device_id": "adapter-mtt", "monitor_device_id": "monitor-mtt",
             "target_name": "MTT Virtual Display",
             "x": virtual_x, "y": 0, "width": 1920, "height": 1080},
        ],
    }


class FakeDesktop:
    def __init__(self, value=topology()):
        self.topology = value
        self.clip = DESKTOP
        self.writes = []

    def read_topology(self):
        return self.topology

    def read_clip(self):
        return self.clip

    def write_clip(self, value):
        self.writes.append(value)
        self.clip = value
        return True

    def controller(self):
        return VerifiedCursorClip(self.read_topology, self.read_clip, self.write_clip)


class VerifiedCursorClipTests(unittest.TestCase):
    def test_start_and_stop_only_change_the_owned_clip(self):
        desktop = FakeDesktop()
        cursor = desktop.controller()
        started = cursor.start()
        self.assertEqual(started["state"], "running")
        self.assertEqual(desktop.clip, PHYSICAL)
        self.assertEqual(cursor.start()["state"], "running")
        stopped = cursor.stop()
        self.assertTrue(stopped["restored"])
        self.assertIsNone(desktop.clip)
        self.assertEqual(desktop.writes, [PHYSICAL, None])

    def test_unverified_or_cloned_topology_fails_without_setting_clip(self):
        for invalid in (topology(verified=False), topology(cloned=True),
                        topology(parent="different-adapter")):
            with self.subTest(invalid=invalid):
                desktop = FakeDesktop(invalid)
                result = desktop.controller().start()
                self.assertEqual(result["error_code"], "TOPOLOGY_UNVERIFIED")
                self.assertEqual(desktop.writes, [])

    def test_virtual_display_overlapping_physical_fails_closed(self):
        desktop = FakeDesktop(topology(virtual_x=2500))
        result = desktop.controller().start()
        self.assertEqual(result["error_code"], "TOPOLOGY_UNVERIFIED")
        self.assertEqual(desktop.writes, [])

    def test_preserves_narrower_external_clip_inside_physical_bounds(self):
        desktop = FakeDesktop()
        external = (50, 80, 400, 500)
        desktop.clip = external
        result = desktop.controller().start()
        self.assertEqual(result["state"], "preserved")
        self.assertEqual(desktop.clip, external)
        self.assertEqual(desktop.writes, [])

    def test_external_clip_outside_physical_bounds_is_not_overwritten(self):
        desktop = FakeDesktop()
        external = (-10, 0, 200, 200)
        desktop.clip = external
        result = desktop.controller().start()
        self.assertEqual(result["error_code"], "EXTERNAL_CLIP_CONFLICT")
        self.assertEqual(desktop.clip, external)
        self.assertEqual(desktop.writes, [])

    def test_stop_does_not_overwrite_a_later_external_clip(self):
        desktop = FakeDesktop()
        cursor = desktop.controller()
        self.assertEqual(cursor.start()["state"], "running")
        external = (10, 10, 300, 300)
        desktop.clip = external
        stopped = cursor.stop()
        self.assertEqual(stopped["error_code"], "OWNED_CLIP_CHANGED")
        self.assertEqual(desktop.clip, external)
        self.assertEqual(desktop.writes, [PHYSICAL])

    def test_failed_post_apply_topology_verification_rolls_back_only_own_clip(self):
        desktop = FakeDesktop()
        reads = 0

        def read_topology():
            nonlocal reads
            reads += 1
            return desktop.topology if reads == 1 else topology(virtual_x=-1800)

        cursor = VerifiedCursorClip(read_topology, desktop.read_clip, desktop.write_clip)
        result = cursor.start()
        self.assertEqual(result["error_code"], "CURSOR_CLIP_VERIFY_FAILED")
        self.assertIsNone(desktop.clip)
        self.assertEqual(desktop.writes, [PHYSICAL, None])
        self.assertEqual(cursor.status()["state"], "off")


if __name__ == "__main__":
    unittest.main()
