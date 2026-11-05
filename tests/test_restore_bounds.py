"""Synthetic Win32 proofs only; never enumerate or move real windows."""
import ctypes
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from modules.app_window_router.restore_bounds import (
    _MonitorInfoExW, resolve_restore_bounds, WS_EX_TOOLWINDOW,
)


class Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


class RestoreBoundsTests(unittest.TestCase):
    def setUp(self):
        self.last_error = 0
        self.style = 0
        self.style_error = 0
        self.monitor = [0, 0, 1920, 1080]
        self.work = [0, 40, 1920, 1080]
        self.device = r"\\.\DISPLAY1"
        self.monitor_handle = 123
        self.info_ok = True
        self.monitor_flags = []

        def set_error(error):
            self.last_error = error

        self.patches = [patch.object(ctypes, "set_last_error", set_error, create=True),
                        patch.object(ctypes, "get_last_error", lambda: self.last_error, create=True)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

        def style(_hwnd, index):
            self.assertEqual(index, -20)
            self.last_error = self.style_error
            return self.style

        def from_window(_hwnd, flags):
            self.monitor_flags.append(flags)
            return self.monitor_handle

        def info(_handle, pointer):
            value = ctypes.cast(pointer, ctypes.POINTER(_MonitorInfoExW)).contents
            self.assertEqual(value.cbSize, ctypes.sizeof(_MonitorInfoExW))
            for target, coords in [(value.rcMonitor, self.monitor), (value.rcWork, self.work)]:
                target.left, target.top, target.right, target.bottom = coords
            value.szDevice = self.device
            return self.info_ok

        self.api = SimpleNamespace(GetWindowLongPtrW=Function(style),
            MonitorFromWindow=Function(from_window), GetMonitorInfoW=Function(info))

    def resolve(self, bounds=None):
        return resolve_restore_bounds(self.api, 0x123456789, bounds or [100, 50, 500, 400])

    def test_top_taskbar_offsets_workspace_without_changing_size(self):
        self.assertEqual(self.resolve(), {"bounds": [100, 90, 500, 440],
            "monitor_name": self.device, "monitor_bounds": self.monitor, "tool_window": False})
        self.assertEqual(self.monitor_flags, [0])

    def test_negative_origin_left_taskbar_uses_difference_not_absolute_origin(self):
        self.monitor = [-1920, -200, 0, 880]
        self.work = [-1880, -200, 0, 880]
        self.assertEqual(self.resolve([-1800, -100, -1400, 400])["bounds"],
                         [-1760, -100, -1360, 400])

    def test_bottom_right_taskbar_does_not_shift_origin(self):
        self.work = [0, 0, 1880, 1040]
        self.assertEqual(self.resolve()["bounds"], [100, 50, 500, 400])

    def test_tool_window_is_already_screen_space_but_needs_monitor_proof(self):
        self.style = WS_EX_TOOLWINDOW
        self.assertEqual(self.resolve()["bounds"], [100, 50, 500, 400])
        self.assertTrue(self.resolve()["tool_window"])
        self.monitor_handle = 0
        self.assertIsNone(self.resolve())

    def test_zero_style_success_and_zero_style_failure_are_distinct(self):
        self.assertIsNotNone(self.resolve())
        self.style_error = 5
        self.assertIsNone(self.resolve())

    def test_bad_or_missing_monitor_evidence_fails_closed(self):
        for field, value in [("monitor_handle", 0), ("info_ok", False),
                             ("device", ""), ("monitor", [0, 0, 0, 1080]),
                             ("work", [-1, 0, 1920, 1080]),
                             ("work", [0, 0, 1920, 1081])]:
            before = getattr(self, field)
            with self.subTest(field=field, value=value):
                setattr(self, field, value)
                self.assertIsNone(self.resolve())
            setattr(self, field, before)

    def test_invalid_or_disconnected_restore_rectangle_fails_closed(self):
        for bounds in [[0, 0, 0, 100], [True, 0, 100, 100], [0, 0, "100", 100],
                       [0, 0, 2 ** 31, 100], [4000, 0, 4100, 100], [0, 0, 10]]:
            with self.subTest(bounds=bounds):
                self.assertIsNone(resolve_restore_bounds(self.api, 1, bounds))

    def test_signatures_are_pointer_safe_and_missing_native_api_is_unknown(self):
        self.resolve()
        self.assertEqual(self.api.GetWindowLongPtrW.restype, ctypes.c_ssize_t)
        self.assertEqual(self.api.MonitorFromWindow.restype, ctypes.wintypes.HANDLE)
        del self.api.GetMonitorInfoW
        self.assertIsNone(self.resolve())

    def test_32bit_style_fallback_only_on_32bit(self):
        self.api.GetWindowLongW = self.api.GetWindowLongPtrW
        del self.api.GetWindowLongPtrW
        actual_sizeof = ctypes.sizeof
        with patch.object(ctypes, "sizeof", side_effect=lambda value:
                          4 if value is ctypes.c_void_p else actual_sizeof(value)):
            self.assertIsNotNone(self.resolve())
        with patch.object(ctypes, "sizeof", side_effect=lambda value:
                          8 if value is ctypes.c_void_p else actual_sizeof(value)):
            self.assertIsNone(self.resolve())


if __name__ == "__main__":
    unittest.main()
