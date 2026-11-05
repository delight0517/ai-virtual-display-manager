from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.app_window_router.physical_guard import (PhysicalWindowGuard, correction_target,
                                                       topology_ready, _diagnostic_app,
                                                       _diagnostic_output)
from modules.app_window_router.module import AppWindowRouter, _display_path_monitor_key, _pnp_monitor_key
from modules.app_window_router.guard_settings import GuardSettingsStore
from core.module_host import ModuleHost


def topology():
    physical = {"display_name": r"\\.\DISPLAY1", "monitor_device_id": "DISPLAY\\DELL#P",
                "monitor_parent": "PCI\\GPU", "virtual": False, "attached": True,
                "verified": True, "cloned": False, "topology_verified": True,
                "monitor_identity_verified": True, "x": 0, "y": 0,
                "width": 1920, "height": 1080}
    virtual = {"display_name": r"\\.\DISPLAY10", "monitor_device_id": "DISPLAY\\MTT#V",
               "monitor_parent": r"ROOT\DISPLAY\0000", "virtual": True, "attached": True,
               "verified": True, "cloned": False, "topology_verified": True,
               "monitor_identity_verified": True, "x": -1920, "y": 0,
               "width": 1920, "height": 1080}
    return {"topology_verified": True, "mtt_device_instance_id": r"ROOT\DISPLAY\0000",
            "displays": [physical, virtual]}


def window(identity="new", bounds=(-1800, 80, -900, 700)):
    return {"window_id": identity, "hwnd": 100, "pid": 42, "bounds": list(bounds),
            "window_state": "normal", "bounds_space": "screen"}


class FakeWindowApi:
    def __init__(self, rows=()):
        self.rows = [dict(row) for row in rows]
        self.moves = []
        self.enumerations = 0

    def enumerate_all_windows(self):
        self.enumerations += 1
        return [dict(row, bounds=list(row["bounds"])) for row in self.rows]

    def move_exact_without_activation(self, expected, bounds):
        current = [r for r in self.rows if r["window_id"] == expected["window_id"]
                   and r["hwnd"] == expected["hwnd"] and r["bounds"] == expected["bounds"]]
        if len(current) != 1:
            return False
        current[0]["bounds"] = list(bounds)
        self.moves.append(list(bounds))
        return True


class PhysicalWindowGuardTests(unittest.TestCase):
    def test_waiting_for_physical_monitor_skips_window_enumeration_and_resumes(self):
        api = FakeWindowApi()
        state = self.fully_identified_topology()
        physical = state["displays"].pop(0)
        guard = PhysicalWindowGuard(api, lambda: state, interval=0.01)
        guard.start()
        try:
            deadline = time.monotonic() + 1
            while (guard.status()["state"] != "waiting_for_physical_monitor"
                   and time.monotonic() < deadline):
                time.sleep(0.01)
            self.assertEqual(guard.status()["state"], "waiting_for_physical_monitor")
            api.rows.append(window("opened-while-waiting"))
            time.sleep(0.08)
            self.assertEqual(api.enumerations, 1)  # startup baseline only
            self.assertEqual(api.moves, [])

            state["displays"].insert(0, physical)
            guard._topology_at = 0
            deadline = time.monotonic() + 1
            while not api.moves and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreater(api.enumerations, 1)
            self.assertEqual(api.moves, [[24, 24, 924, 644]])
        finally:
            guard.stop(timeout=1)

    def test_ordinary_codex_recovers_but_explicit_ai_owner_does_not(self):
        ordinary = window() | {"app": "Codex"}
        self.assertIsNotNone(correction_target(ordinary, topology()))
        self.assertIsNone(correction_target(ordinary | {"ai_task_owned": True}, topology()))
        self.assertIsNone(correction_target(window() | {"class_name": "ConsoleWindowClass"}, topology()))

    def test_virtual_primary_does_not_prevent_sole_physical_recovery(self):
        state = topology()
        state["displays"][0]["x"] = 1920
        state["displays"][1]["x"] = 0
        self.assertTrue(topology_ready(state))
        self.assertEqual(correction_target(window(bounds=(40, 50, 900, 700)), state)["bounds"],
                         [1944, 24, 2804, 674])

    def test_reconnect_recovers_existing_window_without_foreground_change(self):
        state = self.fully_identified_topology()
        api = FakeWindowApi([window("existing")])
        guard = PhysicalWindowGuard(api, lambda: state, interval=0.01)
        guard.start()
        try:
            time.sleep(0.15)
            self.assertEqual(api.moves, [])
            physical = state["displays"].pop(0)
            guard._topology_at = 0
            time.sleep(0.15)
            self.assertEqual(guard.status()["state"], "waiting_for_physical_monitor")
            self.assertEqual(api.moves, [])
            state["displays"].insert(0, physical)
            guard._topology_at = 0
            deadline = time.monotonic() + 1
            while not api.moves and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(api.moves, [[24, 24, 924, 644]])
        finally:
            guard.stop(timeout=1)

    def test_transient_topology_failure_does_not_kill_guard(self):
        broken = threading.Event()
        def provider():
            if broken.is_set():
                raise OSError("display reconnect in progress")
            return self.fully_identified_topology()
        api = FakeWindowApi()
        guard = PhysicalWindowGuard(api, provider, interval=0.01)
        guard.start()
        try:
            broken.set()
            guard._topology_cache = None
            time.sleep(0.15)
            self.assertTrue(guard._thread.is_alive())
            self.assertEqual(api.moves, [])
            api.rows.append(window())
            broken.clear()
            deadline = time.monotonic() + 1
            while not api.moves and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(api.moves, [[24, 24, 924, 644]])
        finally:
            guard.stop(timeout=1)

    @staticmethod
    def fully_identified_topology():
        value = topology()
        for index, display in enumerate(value["displays"]):
            display["adapter_device_id"] = f"adapter-{index}"
            display["monitor_path"] = f"monitor-path-{index}"
        return value

    def test_diagnostics_record_verified_discord_recovery_without_identity(self):
        records = []
        api = FakeWindowApi()
        state = self.fully_identified_topology()
        guard = PhysicalWindowGuard(api, lambda: state, interval=0.01,
                                    diagnostic_recorder=records.append)
        guard.start()
        discord = window("discord", (-1800, 80, -900, 700)) | {
            "app": "Application", "exe_path": r"C:\Program Files\Discord\Discord.exe",
            "foreground": True, "title": "private call title", "pid": 9876}
        api.rows.append(discord)
        deadline = time.monotonic() + 1
        while not any(row.get("event") == "restore_result" for row in records) and time.monotonic() < deadline:
            time.sleep(0.01)
        guard.stop(timeout=1)
        observed = next(row for row in records if row.get("event") == "window_observed")
        restored = next(row for row in records if row.get("event") == "restore_result")
        self.assertEqual((observed["app"], observed["output"]), ("discord", "virtual"))
        self.assertEqual((restored["result"], restored["output"]), ("succeeded", "primary"))
        for row in records:
            self.assertNotIn("title", row)
            self.assertNotIn("pid", row)
            self.assertNotIn("exe_path", row)
            self.assertNotIn("window_id", row)

    def test_minimized_activated_window_is_logged_unknown_then_checked_after_restore(self):
        records = []
        minimized = window("discord-min", (-1800, 0, -900, 600)) | {
            "app": "Application", "exe_path": r"C:\Discord\Discord.exe",
            "window_state": "minimized", "bounds_space": "workspace"}
        api = FakeWindowApi([minimized])
        state = self.fully_identified_topology()
        guard = PhysicalWindowGuard(api, lambda: state, interval=0.01,
                                    diagnostic_recorder=records.append)
        guard.start()
        api.rows[0]["foreground"] = True
        time.sleep(0.16)
        self.assertEqual(api.moves, [])
        first = [row for row in records if row.get("event") == "window_observed"]
        self.assertEqual(len(first), 1)
        self.assertEqual((first[0]["window_state"], first[0]["output"]), ("minimized", "unknown"))
        api.rows[0].update(window_state="normal", bounds_space="screen", foreground=True,
                           bounds=[-1800, 80, -900, 700])
        deadline = time.monotonic() + 1
        while not api.moves and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(api.moves, [[24, 24, 924, 644]])
        guard.stop(timeout=1)

    def test_virtual_frame_straddling_and_oversized_window_recover(self):
        state = topology()
        target = correction_target(window(bounds=(-1927, -7, 7, 1100)), state)
        self.assertEqual(target["bounds"], [24, 24, 1896, 1056])
        self.assertIsNone(correction_target(window(bounds=(-300, 30, 600, 700)), state))
        self.assertIsNone(correction_target(window() | {"ai_task_owned": True}, state))

    def test_only_unambiguous_new_virtual_or_offscreen_window_moves(self):
        state = topology()
        self.assertTrue(topology_ready(state))
        target = correction_target(window(), state)
        self.assertEqual(target["bounds"], [24, 24, 924, 644])
        self.assertIsNone(correction_target(window("old", (20, 20, 900, 600)), state))
        self.assertIsNone(correction_target(window("partial", (-10, 30, 300, 600)), state))
        self.assertIsNone(correction_target(window("min", (-1000, 0, -500, 500)) | {
            "window_state": "minimized"}, state))
        self.assertIsNone(correction_target(window(), {"topology_verified": False, "displays": []}))

    def test_unknown_monitor_identity_never_counts_as_physical_primary(self):
        state = topology()
        state["displays"][0]["monitor_identity_verified"] = False
        self.assertFalse(topology_ready(state))
        self.assertIsNone(correction_target(window(), state))

    def test_active_display_path_and_pnp_instance_alias_normalize_to_same_monitor(self):
        path = r"\\?\DISPLAY#MTT1337#1&28a6823a&0&UID256#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}"
        instance = r"DISPLAY\MTT1337\1&28A6823A&0&UID256"
        self.assertEqual(_display_path_monitor_key(path), _pnp_monitor_key(instance))

    def test_guard_uses_baseline_and_moves_only_later_new_window_then_stops(self):
        api = FakeWindowApi([window("existing")])
        guard = PhysicalWindowGuard(api, topology, interval=0.02)
        self.assertEqual(guard.start()["state"], "running")
        api.rows.append(window())
        deadline = time.monotonic() + 1.0
        while not api.moves and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(api.moves, [[24, 24, 924, 644]])
        stopped = guard.stop(timeout=1.0)
        self.assertEqual(stopped["state"], "off")
        self.assertFalse(stopped["enabled"])

    def test_new_minimized_window_is_checked_once_after_restore(self):
        api = FakeWindowApi()
        guard = PhysicalWindowGuard(api, topology, interval=0.02)
        guard.start()
        minimized = window("new-minimized", (-1800, 0, -900, 600))
        minimized.update(window_state="minimized", bounds_space="workspace")
        api.rows.append(minimized)
        time.sleep(0.06)
        self.assertEqual(api.moves, [])
        api.rows[0].update(window_state="normal", bounds_space="screen",
                           bounds=[-1800, 80, -900, 680])
        deadline = time.monotonic() + 1
        while not api.moves and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(api.moves, [[24, 24, 924, 624]])
        guard.stop(timeout=1)

    def test_existing_minimized_window_remains_excluded_after_restore(self):
        existing = window("existing-minimized", (-1800, 0, -900, 600))
        existing.update(window_state="minimized", bounds_space="workspace")
        api = FakeWindowApi([existing])
        guard = PhysicalWindowGuard(api, topology, interval=0.02)
        guard.start()
        api.rows[0].update(window_state="normal", bounds_space="screen",
                           bounds=[-1800, 80, -900, 680])
        time.sleep(0.08)
        self.assertEqual(api.moves, [])
        guard.stop(timeout=1)

    def test_guard_defaults_off_and_stop_blocks_subsequent_movements(self):
        api = FakeWindowApi()
        guard = PhysicalWindowGuard(api, topology, interval=0.02)
        self.assertEqual(guard.status()["state"], "off")
        guard.start()
        self.assertEqual(guard.stop(timeout=1)["state"], "off")
        api.rows.append(window())
        time.sleep(0.08)
        self.assertEqual(api.moves, [])

    def test_stop_timeout_is_bounded_and_inflight_move_does_not_restart_guard(self):
        class BlockingApi(FakeWindowApi):
            def __init__(self):
                super().__init__()
                self.entered = threading.Event()
                self.release = threading.Event()

            def move_exact_without_activation(self, expected, bounds):
                self.entered.set()
                self.release.wait(2)
                return super().move_exact_without_activation(expected, bounds)

        api = BlockingApi()
        guard = PhysicalWindowGuard(api, topology, interval=0.01)
        guard.start()
        api.rows.append(window())
        self.assertTrue(api.entered.wait(1))
        started = time.monotonic()
        state = guard.stop(timeout=0.02)
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual(state["state"], "stop_unverified")
        api.release.set()
        deadline = time.monotonic() + 1
        while guard._thread and guard._thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(guard.stop(timeout=0.5)["state"], "off")

    def test_settings_store_is_atomic_opt_in_and_preserves_other_preferences(self):
        import tempfile, json
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text('{"theme":"dark"}', encoding="utf-8")
            store = GuardSettingsStore(path)
            self.assertEqual(store.read(), (False, None))
            store.write(True)
            self.assertEqual(store.read(), (True, None))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["theme"], "dark")
            store.write(False)
            self.assertEqual(store.read(), (False, None))

    def test_settings_store_defaults_and_unreadable_values_fail_closed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            store = GuardSettingsStore(path)
            self.assertEqual(store.read(), (False, None))
            path.write_text("not-json", encoding="utf-8")
            enabled, error = store.read()
            self.assertFalse(enabled)
            self.assertTrue(error)
            path.write_text('{"physical_window_guard_enabled":"yes"}', encoding="utf-8")
            enabled, error = store.read()
            self.assertFalse(enabled)
            self.assertTrue(error)

    def test_versioned_host_can_query_guard_status_without_starting_or_enumerating(self):
        host = ModuleHost(ROOT)
        host.load("app_window_router")
        status = host.invoke("app_window_router", "physical_guard_status")
        self.assertFalse(status["runtime"]["enabled"])
        self.assertEqual(status["runtime"]["state"], "off")
        self.assertIsInstance(status["preference_enabled"], bool)
        host.dispose_all()

    def test_closing_without_an_armed_guard_does_not_write_a_settings_file(self):
        class Store:
            writes = []
            def read(self):
                return False, None
            def write(self, enabled):
                self.writes.append(enabled)
        store = Store()
        router = AppWindowRouter(window_api=object(), guard_settings_store_factory=lambda: store)
        result = router.action("physical_guard_stop", {})
        self.assertFalse(result["runtime"]["enabled"])
        self.assertEqual(store.writes, [])


if __name__ == "__main__":
    unittest.main()
