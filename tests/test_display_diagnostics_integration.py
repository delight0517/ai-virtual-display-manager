from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.app_window_router.module import AppWindowRouter
from modules.app_window_router.guard_settings import GuardSettingsStore
from modules.display_diagnostics.module import DisplayDiagnosticsModule, DisplayDiagnosticStore


class FakeWindowApi:
    def enumerate_all_windows(self):
        return []


def verified_topology():
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
    for index, display in enumerate((physical, virtual)):
        display["adapter_device_id"] = f"adapter-{index}"
        display["monitor_path"] = f"path-{index}"
    return {"topology_verified": True, "mtt_device_instance_id": r"ROOT\DISPLAY\0000",
            "displays": [physical, virtual]}


class DisplayDiagnosticsIntegrationTests(unittest.TestCase):
    def test_router_guard_lifecycle_writes_only_to_isolated_diagnostic_store(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = DisplayDiagnosticStore(root / "events.jsonl")
            diagnostics = DisplayDiagnosticsModule(lambda: store)
            diagnostics.init()
            state = verified_topology()
            router = AppWindowRouter(
                window_api=FakeWindowApi(),
                display_provider=lambda: state,
                guard_settings_store_factory=lambda: GuardSettingsStore(root / "settings.json"),
            )
            router.bind_diagnostics(lambda event: diagnostics.action("record", event))
            with patch("modules.app_window_router.module.current_guard_topology", return_value=state):
                initial = router.physical_guard_action("physical_guard_status", {})
                self.assertFalse(initial["preference_enabled"])
                started = router.physical_guard_action("physical_guard_start", {"confirmed": True})
                self.assertTrue(started["runtime"]["enabled"])
                stopped = router.physical_guard_action("physical_guard_stop", {})
                self.assertFalse(stopped["runtime"]["enabled"])
            events = store.read()["events"]
            settings = [event["enabled"] for event in events
                        if event["event"] == "guard_state"
                        and event.get("setting") == "physical_window_guard_enabled"]
            self.assertEqual(settings, [False, True, False])
            self.assertTrue(any(event.get("event") == "topology_state"
                                and event.get("topology") == "physical_and_virtual" for event in events))
            self.assertTrue(any(event.get("event") == "guard_start_result"
                                and event.get("result") == "succeeded" for event in events))
            self.assertTrue(any(event.get("event") == "guard_start_result"
                                and event.get("result") == "closed" for event in events))
            self.assertTrue((root / "settings.json").is_file())
            self.assertTrue((root / "events.jsonl").is_file())
            diagnostics.dispose()
            router.dispose()


if __name__ == "__main__":
    unittest.main()
