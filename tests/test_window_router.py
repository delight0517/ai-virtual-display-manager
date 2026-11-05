from __future__ import annotations

import ctypes
import sys
import unittest
from unittest.mock import Mock
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.app_window_router.module import (
    AppWindowRouter, Win32WindowAPI, _is_unowned_root_window,
    classify_app_path, physical_pixel_context,
)


def topology(*, cloned=False, verified=True, duplicate=False, multiple=False):
    mtt_instance = r"ROOT\DISPLAY\0000"
    vdd = {"display_name": r"\\.\DISPLAY10", "target_name": "VDD by MTT",
           "adapter_device_id": r"Root\MttVDD", "attached": True,
           "topology_verified": verified, "verified": verified, "cloned": cloned,
           "monitor_identity_verified": verified,
           "virtual": True, "monitor_parent": mtt_instance,
           "monitor_path": r"\\?\DISPLAY#MTT#0000#{GUID}",
           "x": -2560, "y": 0, "width": 2560, "height": 1440}
    displays = [{"display_name": r"\\.\DISPLAY1", "adapter_device_id": "PCI\\NVIDIA",
                 "attached": True, "topology_verified": verified, "verified": verified,
                 "cloned": False, "x": 0, "y": 0, "width": 2560, "height": 1440},
                {**vdd, "cloned": cloned}]
    if duplicate:
        displays.append({**vdd, "display_name": r"\\.\DISPLAY11"})
    elif multiple:
        displays.append({**vdd, "display_name": r"\\.\DISPLAY11",
                         "monitor_path": r"\\?\DISPLAY#MTT#0001#{GUID}",
                         "x": -5120})
    return {"topology_verified": verified, "mtt_device_instance_id": mtt_instance,
            "displays": displays}


def virtual_only_topology():
    display = dict(topology()["displays"][1])
    display.update(x=0, y=0)
    return {"topology_verified": True, "mtt_device_instance_id": r"ROOT\DISPLAY\0000",
            "displays": [display]}


def onlychat_window():
    return {"window_id": "a" * 64, "app": "Only Chat", "pid": 41084,
            "hwnd": 63906082, "process_created": "123456", "class_name": "Chrome_WidgetWin_1",
            "exe_path": r"C:\Users\qa\AppData\Local\Programs\OnlyChat-Electron-1.0.5\Only Chat.exe",
            "bounds": [384, 126, 1664, 978]}


class FakeWindows:
    def __init__(self, windows=None):
        self.windows = list(windows if windows is not None else [onlychat_window()])
        self.moves = []
        self.restores = []
        self.change_before_move = False
        self.change_before_restore = False
        self.corrupt_after_move = False

    def enumerate_windows(self):
        return [dict(w, bounds=list(w["bounds"])) for w in self.windows]

    def move_without_activation(self, hwnd, bounds):
        self.moves.append((hwnd, list(bounds)))
        target = next(w for w in self.windows if w["hwnd"] == hwnd)
        old = target["bounds"]
        width, height = old[2] - old[0], old[3] - old[1]
        target["bounds"] = [bounds[0], bounds[1], bounds[0] + width, bounds[1] + height]
        if self.corrupt_after_move:
            target["bounds"][0] += 1
            target["bounds"][2] += 1
            self.corrupt_after_move = False
        return True

    def move_exact_without_activation(self, expected, bounds):
        if self.change_before_move:
            self.windows[0]["window_id"] = "f" * 64
            return False
        current = [w for w in self.windows if w["hwnd"] == expected["hwnd"]
                   and w["window_id"] == expected["window_id"]
                   and w["bounds"] == expected["bounds"]]
        return len(current) == 1 and self.move_without_activation(current[0]["hwnd"], bounds)

    def restore_exact(self, expected):
        if self.change_before_restore:
            self.windows[0]["window_id"] = "f" * 64
            return False
        current = [w for w in self.windows if w.get("hwnd") == expected.get("hwnd")
                   and w.get("window_id") == expected.get("window_id")
                   and w.get("pid") == expected.get("pid")
                   and w.get("process_created") == expected.get("process_created")
                   and w.get("window_state") == "minimized"]
        if len(current) != 1:
            return False
        self.restores.append(current[0]["hwnd"])
        current[0]["window_state"] = "normal"
        return True


class WindowRouterTests(unittest.TestCase):
    def test_generic_new_window_revalidation_uses_generic_enumerator(self):
        api = Win32WindowAPI.__new__(Win32WindowAPI)
        expected = {"app": "Application", "hwnd": 81, "window_id": "b" * 64,
                    "bounds": [-1800, 20, -900, 500], "window_state": "normal"}
        api.enumerate_all_windows = Mock(return_value=[dict(expected)])
        api.enumerate_windows = Mock(return_value=[])
        api.move_without_activation = Mock(return_value=True)
        self.assertTrue(api.move_exact_without_activation(expected, [24, 24, 924, 504]))
        api.enumerate_all_windows.assert_called_once_with()
        api.enumerate_windows.assert_not_called()

    def test_physical_pixel_context_is_restored_after_success(self):
        user32 = Mock()
        previous = wintypes.HANDLE(0x1234)
        user32.SetThreadDpiAwarenessContext.side_effect = [previous, wintypes.HANDLE(0x5678)]
        with physical_pixel_context(user32):
            pass
        calls = user32.SetThreadDpiAwarenessContext.call_args_list
        self.assertEqual(calls[0].args[0].value, wintypes.HANDLE(-4).value)
        self.assertEqual(calls[1].args[0].value, previous.value)

    def test_physical_pixel_context_fails_closed_if_context_cannot_be_set(self):
        user32 = Mock()
        user32.SetThreadDpiAwarenessContext.return_value = None
        with self.assertRaises(OSError):
            with physical_pixel_context(user32):
                self.fail("must not enumerate or move in an unverified coordinate context")
        user32.SetThreadDpiAwarenessContext.assert_called_once()

    def test_physical_pixel_context_fails_if_restore_is_rejected(self):
        user32 = Mock()
        user32.SetThreadDpiAwarenessContext.side_effect = [
            wintypes.HANDLE(0x1234), None,
        ]
        with self.assertRaises(OSError):
            with physical_pixel_context(user32):
                pass

    def test_real_windows_dpi_context_changes_and_restores(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        get_context = user32.GetThreadDpiAwarenessContext
        get_context.argtypes = []
        get_context.restype = wintypes.HANDLE
        get_awareness = user32.GetAwarenessFromDpiAwarenessContext
        get_awareness.argtypes = [wintypes.HANDLE]
        get_awareness.restype = ctypes.c_int
        previous_awareness = get_awareness(get_context())
        with physical_pixel_context(user32):
            self.assertEqual(get_awareness(get_context()), 2)  # PER_MONITOR_AWARE
        self.assertEqual(get_awareness(get_context()), previous_awareness)

    def test_win32_root_and_owner_checks_have_pointer_safe_signatures(self):
        user32 = Mock()
        user32.GetAncestor.return_value = 123
        user32.GetWindow.return_value = 0
        self.assertTrue(_is_unowned_root_window(user32, 123))
        self.assertEqual(user32.GetAncestor.argtypes, [wintypes.HWND, wintypes.UINT])
        self.assertEqual(user32.GetAncestor.restype, wintypes.HWND)
        self.assertEqual(user32.GetWindow.argtypes, [wintypes.HWND, wintypes.UINT])
        self.assertEqual(user32.GetWindow.restype, wintypes.HWND)
        user32.GetWindow.return_value = 456
        self.assertFalse(_is_unowned_root_window(user32, 123))

    def test_window_placement_api_uses_pointer_safe_signature(self):
        user32, kernel32 = Mock(), Mock()
        api = Win32WindowAPI(user32, kernel32)
        self.assertEqual(user32.GetWindowPlacement.argtypes[0], wintypes.HWND)
        self.assertEqual(user32.GetWindowPlacement.argtypes[1]._type_,
                         ctypes.POINTER(api.WINDOWPLACEMENT)._type_)
        self.assertEqual(user32.GetWindowPlacement.restype, wintypes.BOOL)

    def test_move_rejects_window_identity_change_immediately_before_setwindowpos(self):
        api = FakeWindows()
        api.change_before_move = True
        router = AppWindowRouter(api, lambda: topology())
        with self.assertRaisesRegex(RuntimeError, "이동 직전 창 identity/위치"):
            router.action("move_window", {"window_id": "a" * 64,
                                          "display_name": r"\\.\DISPLAY10",
                                          "confirmed": True})
        self.assertEqual(api.moves, [])

    def test_only_exact_supported_install_roots_are_allowlisted(self):
        self.assertEqual(classify_app_path(onlychat_window()["exe_path"]), "Only Chat")
        self.assertEqual(classify_app_path(
            r"C:\Program Files\WindowsApps\OpenAI.Codex_26.1023.6621.0_x64__abc\app\ChatGPT.exe"),
            "Codex")
        self.assertIsNone(classify_app_path(r"C:\Temp\Only Chat.exe"))
        self.assertIsNone(classify_app_path(r"C:\Program Files\WindowsApps\Fake.ChatGPT\app\ChatGPT.exe"))

    def test_listing_exposes_only_metadata_without_titles_or_content(self):
        api = FakeWindows()
        router = AppWindowRouter(api, lambda: topology())
        result = router.action("list_windows", {})
        self.assertEqual([w["app"] for w in result["windows"]], ["Only Chat"])
        self.assertNotIn("title", result["windows"][0])
        self.assertNotIn("text", result["windows"][0])
        self.assertEqual(result["virtual_outputs"][0]["display_name"], r"\\.\DISPLAY10")
        self.assertTrue(result["movement_requires_confirmation"])
        self.assertTrue(result["restoration_requires_confirmation"])

    def test_listing_and_restore_are_available_without_verified_mtt_output(self):
        api = FakeWindows([{**onlychat_window(), "window_state": "minimized"}])
        router = AppWindowRouter(api, lambda: topology(verified=False))
        result = router.action("list_windows", {})
        self.assertEqual(len(result["windows"]), 1)
        self.assertEqual(result["virtual_outputs"], [])
        self.assertTrue(router.action("restore_window", {
            "window_id": "a" * 64, "confirmed": True,
        })["ok"])
        self.assertEqual(api.restores, [63906082])
        with self.assertRaisesRegex(RuntimeError, "화면 출력 경로"):
            router.action("move_window", {
                "window_id": "a" * 64, "display_name": r"\\.\DISPLAY10",
                "confirmed": True,
            })
        self.assertEqual(api.moves, [])

    def test_restore_requires_explicit_confirmation_and_minimized_allowlisted_window(self):
        api = FakeWindows([{**onlychat_window(), "window_state": "minimized"}])
        router = AppWindowRouter(api, lambda: topology())
        with self.assertRaises(PermissionError):
            router.action("restore_window", {"window_id": "a" * 64, "confirmed": False})
        self.assertEqual(api.restores, [])
        api.windows[0]["window_state"] = "normal"
        with self.assertRaisesRegex(RuntimeError, "더 이상 최소화"):
            router.action("restore_window", {"window_id": "a" * 64, "confirmed": True})
        api.windows[0]["window_state"] = "minimized"
        api.windows[0]["exe_path"] = r"C:\Temp\Only Chat.exe"
        with self.assertRaises(PermissionError):
            router.action("restore_window", {"window_id": "a" * 64, "confirmed": True})
        self.assertEqual(api.restores, [])

    def test_restore_revalidates_exact_window_before_showing_it(self):
        api = FakeWindows([{**onlychat_window(), "window_state": "minimized"}])
        api.change_before_restore = True
        router = AppWindowRouter(api, lambda: topology())
        with self.assertRaisesRegex(RuntimeError, "복원됐는지 확인"):
            router.action("restore_window", {"window_id": "a" * 64, "confirmed": True})
        self.assertEqual(api.restores, [])

    def test_win32_restore_calls_show_restore_only_after_fresh_identity_match(self):
        api = Win32WindowAPI.__new__(Win32WindowAPI)
        api.user32 = Mock()
        api.user32.ShowWindowAsync.return_value = 1
        expected = {**onlychat_window(), "window_state": "minimized"}
        api.enumerate_windows = Mock(side_effect=[
            [dict(expected)], [dict(expected, window_state="normal")],
        ])
        self.assertTrue(api.restore_exact(expected))
        self.assertEqual(api.user32.ShowWindowAsync.call_count, 1)
        call = api.user32.ShowWindowAsync.call_args
        self.assertEqual(call.args[0].value, expected["hwnd"])
        self.assertEqual(call.args[1], 9)
        self.assertEqual(api.user32.ShowWindowAsync.argtypes, [wintypes.HWND, ctypes.c_int])
        self.assertEqual(api.user32.ShowWindowAsync.restype, wintypes.BOOL)
        api.user32.ShowWindowAsync.reset_mock()
        api.enumerate_windows = Mock(return_value=[dict(expected, process_created="changed")])
        self.assertFalse(api.restore_exact(expected))
        api.user32.ShowWindowAsync.assert_not_called()

    def test_window_crossing_output_boundary_is_not_reported_as_on_virtual_output(self):
        api = FakeWindows([dict(onlychat_window(), bounds=[2400, 100, 2700, 400])])
        router = AppWindowRouter(api, lambda: virtual_only_topology())
        result = router.action("list_windows", {})
        self.assertEqual(result["windows"][0]["display_name"], "")

    def test_window_wholly_contained_in_virtual_output_is_classified(self):
        api = FakeWindows([dict(onlychat_window(), bounds=[100, 100, 500, 400])])
        router = AppWindowRouter(api, lambda: virtual_only_topology())
        result = router.action("list_windows", {})
        self.assertEqual(result["windows"][0]["display_name"], r"\\.\DISPLAY10")

    def test_minimized_workspace_coordinates_are_not_classified_as_screen_output(self):
        codex = {**onlychat_window(), "app": "Codex", "window_id": "c" * 64,
                 "hwnd": 788210, "window_state": "minimized",
                 "exe_path": r"C:\Program Files\WindowsApps\OpenAI.Codex_26.1023.6621.0_x64__abc\app\ChatGPT.exe",
                 "bounds": [370, 58, 1968, 1021]}
        api = FakeWindows([codex])
        router = AppWindowRouter(api, lambda: virtual_only_topology())
        result = router.action("list_windows", {})
        self.assertEqual(result["windows"][0]["window_state"], "minimized")
        self.assertEqual(result["windows"][0]["display_name"], "")

    def test_minimized_window_is_never_misreported_as_already_on_target(self):
        codex = {**onlychat_window(), "app": "Codex", "window_id": "c" * 64,
                 "hwnd": 788210, "window_state": "minimized",
                 "exe_path": r"C:\Program Files\WindowsApps\OpenAI.Codex_26.1023.6621.0_x64__abc\app\ChatGPT.exe",
                 "bounds": [370, 58, 1968, 1021]}
        api = FakeWindows([codex])
        router = AppWindowRouter(api, lambda: virtual_only_topology())
        with self.assertRaisesRegex(RuntimeError, "최소화된 창의 복원 위치는 모니터 좌표로 확인되지 않습니다"):
            router.action("move_window", {"window_id": "c" * 64,
                                           "display_name": r"\\.\DISPLAY10",
                                           "confirmed": True})
        self.assertEqual(api.moves, [])

    def test_minimized_window_off_target_is_not_moved_or_restored(self):
        codex = {**onlychat_window(), "app": "Codex", "window_id": "c" * 64,
                 "hwnd": 788210, "window_state": "minimized",
                 "exe_path": r"C:\Program Files\WindowsApps\OpenAI.Codex_26.1023.6621.0_x64__abc\app\ChatGPT.exe",
                 "bounds": [3000, 58, 4598, 1021]}
        api = FakeWindows([codex])
        router = AppWindowRouter(api, lambda: virtual_only_topology())
        with self.assertRaisesRegex(RuntimeError, "최소화된 창의 복원 위치는 모니터 좌표로 확인되지 않습니다"):
            router.action("move_window", {"window_id": "c" * 64,
                                           "display_name": r"\\.\DISPLAY10",
                                           "confirmed": True})
        self.assertEqual(api.moves, [])

    def test_move_requires_confirmation_and_never_calls_window_api(self):
        api = FakeWindows()
        router = AppWindowRouter(api, lambda: topology())
        with self.assertRaises(PermissionError):
            router.action("move_window", {"window_id": "a" * 64,
                                           "display_name": r"\\.\DISPLAY10",
                                           "confirmed": False})
        self.assertEqual(api.moves, [])

    def test_move_rechecks_exact_window_and_places_only_it_without_activation(self):
        api = FakeWindows()
        router = AppWindowRouter(api, lambda: topology())
        result = router.action("move_window", {"window_id": "a" * 64,
                                                "display_name": r"\\.\DISPLAY10",
                                                "confirmed": True})
        self.assertEqual(api.moves, [(63906082, [-2528, 32, -1248, 884])])
        self.assertEqual(result["bounds"], [-2528, 32, -1248, 884])
        self.assertFalse(result["activated"])
        self.assertFalse(result["data_or_content_read"])
        self.assertEqual(api.windows[0]["bounds"][2] - api.windows[0]["bounds"][0], 1280)
        self.assertEqual(api.windows[0]["bounds"][3] - api.windows[0]["bounds"][1], 852)

    def test_stale_window_id_fails_without_moving_any_window(self):
        api = FakeWindows([dict(onlychat_window(), window_id="b" * 64)])
        router = AppWindowRouter(api, lambda: topology())
        with self.assertRaisesRegex(RuntimeError, "바뀌었거나 닫혔습니다"):
            router.action("move_window", {"window_id": "a" * 64,
                                           "display_name": r"\\.\DISPLAY10",
                                           "confirmed": True})
        self.assertEqual(api.moves, [])

    def test_unknown_or_cloned_output_fails_closed(self):
        for state in (topology(verified=False), topology(cloned=True), topology(duplicate=True)):
            api = FakeWindows()
            router = AppWindowRouter(api, lambda state=state: state)
            with self.subTest(state=state):
                listed = router.action("list_windows", {})
                self.assertEqual(len(listed["windows"]), 1)
                self.assertEqual(listed["virtual_outputs"], [])
                with self.assertRaises(RuntimeError):
                    router.action("move_window", {
                        "window_id": "a" * 64,
                        "display_name": r"\\.\DISPLAY10",
                        "confirmed": True,
                    })
            self.assertEqual(api.moves, [])

    def test_distinct_verified_mtt_outputs_are_both_available(self):
        state = topology(multiple=True)
        api = FakeWindows()
        router = AppWindowRouter(api, lambda: state)
        result = router.action("list_windows", {})
        self.assertEqual([row["display_name"] for row in result["virtual_outputs"]],
                         [r"\\.\DISPLAY10", r"\\.\DISPLAY11"])

    def test_idle_window_counter_returns_count_only_and_ignores_minimized(self):
        state = topology(multiple=True)
        api = FakeWindows()
        api.enumerate_all_windows = Mock(return_value=[
            {"pid": 444, "bounds": [-5000, 30, -4900, 100], "window_state": "normal"},
            {"pid": 445, "bounds": [0, 0, 50, 50], "window_state": "minimized",
             "bounds_space": "workspace"},
            {"pid": 446, "bounds": [500, 500, 510, 510], "window_state": "normal"},
        ])
        router = AppWindowRouter(api, lambda: state)
        result = router.action("count_virtual_output_windows", {})
        self.assertEqual(result, {"active_window_count": 1, "outputs_checked": 2,
                                  "evidence": "bounds_only_no_titles_or_pixels"})

    def test_idle_window_counter_fails_closed_if_output_identity_ambiguous(self):
        state = topology(duplicate=True)
        api = FakeWindows()
        api.enumerate_all_windows = Mock()
        router = AppWindowRouter(api, lambda: state)
        with self.assertRaisesRegex(RuntimeError, "No uncloned MTT"):
            router.action("count_virtual_output_windows", {})
        api.enumerate_all_windows.assert_not_called()

    def test_activity_ignores_exact_explorer_shell_and_all_minimized_windows(self):
        import os
        state = topology()
        shell = {"pid": 444, "bounds": [-2500, 0, -2400, 40], "window_state": "normal",
                 "class_name": "Shell_SecondaryTrayWnd", "exe_path": os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe")}
        minimized = {"pid":445,"bounds":[0,0,100,100],"window_state":"minimized","bounds_space":"workspace",
                     "restore_bounds_evidence":{"monitor_name":r"\\.\DISPLAY1", "monitor_bounds":[0,0,2560,1440], "bounds":[10,30,200,200]}}
        api=FakeWindows();api.enumerate_all_windows=Mock(return_value=[shell,minimized])
        router=AppWindowRouter(api,lambda:state)
        self.assertEqual(router.count_virtual_output_windows()["active_window_count"],0)
        shell["class_name"]="CabinetWClass"
        self.assertEqual(router.count_virtual_output_windows()["active_window_count"],1)
        shell["class_name"]="Shell_SecondaryTrayWnd";shell["exe_path"]=r"C:\other\explorer.exe"
        self.assertEqual(router.count_virtual_output_windows()["active_window_count"],1)
        minimized["restore_bounds_evidence"]["monitor_bounds"]=[0,0,1920,1080]
        self.assertEqual(router.count_virtual_output_windows()["active_window_count"],1)

    def test_activity_retains_unknown_nonminimized_but_ignores_minimized_virtual_window(self):
        state=topology();api=FakeWindows()
        api.enumerate_all_windows=Mock(return_value=[
            {"pid":0,"bounds":None,"window_state":"unknown","bounds_space":"unknown"},
            {"pid":445,"bounds":[0,0,50,50],"window_state":"minimized","bounds_space":"workspace",
             "restore_bounds_evidence":{"monitor_name":r"\\.\DISPLAY10","monitor_bounds":[-2560,0,0,1440],"bounds":[-2500,50,-2400,150]}}
        ])
        self.assertEqual(AppWindowRouter(api,lambda:state).count_virtual_output_windows()["active_window_count"],1)

    def test_window_that_does_not_fit_output_is_not_resized_or_moved(self):
        api = FakeWindows()
        small = topology()
        small["displays"][1]["width"] = 1000
        small["displays"][1]["height"] = 700
        router = AppWindowRouter(api, lambda: small)
        with self.assertRaisesRegex(RuntimeError, "보다 큽니다"):
            router.action("move_window", {"window_id": "a" * 64,
                                           "display_name": r"\\.\DISPLAY10",
                                           "confirmed": True})
        self.assertEqual(api.moves, [])

    def test_post_move_failure_never_rolls_back_to_physical_origin(self):
        api = FakeWindows()
        results = [topology(), topology(verified=False)]
        router = AppWindowRouter(api, lambda: results.pop(0))
        with self.assertRaisesRegex(RuntimeError, "화면 출력 경로"):
            router.action("move_window", {"window_id": "a" * 64,
                                           "display_name": r"\\.\DISPLAY10",
                                           "confirmed": True})
        self.assertEqual(api.moves[0], (63906082, [-2528, 32, -1248, 884]))
        self.assertEqual(len(api.moves), 1)
        self.assertEqual(api.windows[0]["bounds"], [-2528, 32, -1248, 884])

    def test_post_move_failure_rolls_back_only_within_same_verified_mtt_output(self):
        api = FakeWindows()
        api.windows[0]["bounds"] = [-2500, 100, -1220, 952]
        api.corrupt_after_move = True
        router = AppWindowRouter(api, lambda: topology())
        with self.assertRaisesRegex(RuntimeError, "이동 후 창 좌표"):
            router.action("move_window", {"window_id": "a" * 64,
                                           "display_name": r"\\.\DISPLAY10",
                                           "confirmed": True})
        self.assertEqual(len(api.moves), 2)
        self.assertEqual(api.windows[0]["bounds"], [-2500, 100, -1220, 952])

    def test_post_move_rejects_same_display_name_with_changed_endpoint_identity(self):
        before = virtual_only_topology()
        before["displays"][0]["monitor_path"] = r"\\?\DISPLAY#MTT1337#A"
        after = virtual_only_topology()
        after["displays"][0]["monitor_path"] = r"\\?\DISPLAY#MTT1337#B"
        states = [before, after, after]
        api = FakeWindows()
        router = AppWindowRouter(api, lambda: states.pop(0))
        with self.assertRaisesRegex(RuntimeError, "이동 후 가상 출력 경로"):
            router.action("move_window", {"window_id": "a" * 64,
                                           "display_name": r"\\.\DISPLAY10",
                                           "confirmed": True})
        # Verification failed and the endpoint changed, so rollback is not safe either.
        self.assertEqual(len(api.moves), 1)

    def test_output_identity_fails_closed_when_endpoint_evidence_disappears(self):
        before = virtual_only_topology()
        before["displays"][0]["monitor_path"] = r"\\?\DISPLAY#MTT1337#A"
        after = virtual_only_topology()
        after["displays"][0]["monitor_path"] = r"\\?\DISPLAY#MTT1337#A"
        after["displays"][0].pop("monitor_path")
        states = [before, after, after]
        api = FakeWindows()
        router = AppWindowRouter(api, lambda: states.pop(0))
        with self.assertRaisesRegex(RuntimeError, "복제되지 않은 MTT 가상 출력"):
            router.action("move_window", {"window_id": "a" * 64,
                                           "display_name": r"\\.\DISPLAY10",
                                           "confirmed": True})
        self.assertEqual(len(api.moves), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)



