import unittest

from modules.display_topology.module import (
    DISPLAY_DEVICE_ATTACHED_TO_DESKTOP,
    DISPLAY_DEVICE_MIRRORING_DRIVER,
    DISPLAY_DEVICE_PRIMARY_DEVICE,
    is_primary_display,
)


class DisplayPrimaryFlagTests(unittest.TestCase):
    def test_primary_flag_is_reported(self):
        flags = DISPLAY_DEVICE_ATTACHED_TO_DESKTOP | DISPLAY_DEVICE_PRIMARY_DEVICE
        self.assertTrue(is_primary_display(flags))

    def test_non_primary_attached_display_is_not_reported_primary(self):
        self.assertFalse(is_primary_display(DISPLAY_DEVICE_ATTACHED_TO_DESKTOP))

    def test_mirroring_flag_does_not_imply_primary(self):
        flags = DISPLAY_DEVICE_ATTACHED_TO_DESKTOP | DISPLAY_DEVICE_MIRRORING_DRIVER
        self.assertFalse(is_primary_display(flags))

    def test_unknown_and_empty_flags_are_not_primary(self):
        self.assertFalse(is_primary_display(0))
        self.assertFalse(is_primary_display(DISPLAY_DEVICE_PRIMARY_DEVICE - 1))


if __name__ == "__main__":
    unittest.main()
