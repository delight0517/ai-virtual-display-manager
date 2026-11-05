from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.module_host import ModuleHost
from modules.display_diagnostics.module import DisplayDiagnosticStore


class DisplayDiagnosticStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "display-events.jsonl"
        self.now = datetime.now(timezone.utc)
        self.store = DisplayDiagnosticStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_records_allowlisted_metadata_and_converts_bounds_to_size(self):
        result = self.store.append({"event": "restore_result", "timestamp": self.now,
                                    "app": "discord", "window_state": "normal",
                                    "output": "virtual", "result": "succeeded",
                                    "bounds": [-2048, 20, 0, 1100], "title": "secret",
                                    "window_id": "private", "pid": 1234, "text": "private"})
        self.assertTrue(result["stored"])
        event = self.store.read()["events"][0]
        self.assertEqual(event["bounds"], [-2048, 20, 0, 1100])
        self.assertNotIn("title", event)
        self.assertNotIn("window_id", event)
        self.assertNotIn("pid", event)
        self.assertNotIn("text", event)
        self.assertFalse(self.store.read()["content_recorded"])

    def test_rejects_free_text_identity_and_arbitrary_fields(self):
        for field, value in (("app", "Discord.exe"), ("reason", "raw exception text"),
                             ("setting", "account"), ("bounds", [1, 2, 1, 4])):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.store.append({"event": "window_observed", field: value})

    def test_records_only_allowlisted_topology_summary(self):
        result = self.store.append({"event": "topology_state", "topology": "physical_and_virtual",
                                    "output_count": 2, "result": "succeeded",
                                    "adapter_device_id": "private", "monitor_path": "private"})
        self.assertTrue(result["stored"])
        row = self.store.read()["events"][0]
        self.assertEqual((row["topology"], row["output_count"]), ("physical_and_virtual", 2))
        self.assertNotIn("adapter_device_id", row)
        self.assertNotIn("monitor_path", row)

    def test_monitor_failure_details_are_bounded_and_redacted(self):
        result = self.store.append({
            "event": "topology_state", "timestamp": datetime.now(timezone.utc),
            "topology": "unknown", "output_count": 0, "result": "unavailable",
            "reason": "monitor_identity_refresh_pending",
            "monitor_identity_refresh_state": "refreshing",
            "monitor_identity_age_ms": 5001, "monitor_identity_cache_hit": True,
            "display_count": 1, "identified_display_count": 0,
            "provider_error_type": "TimeoutError", "monitor_path": "private",
        })
        row = self.store.read()["events"][0]
        self.assertTrue(result["stored"])
        self.assertEqual(row["reason"], "monitor_identity_refresh_pending")
        self.assertEqual(row["monitor_identity_refresh_state"], "refreshing")
        self.assertEqual(row["provider_error_type"], "TimeoutError")
        self.assertNotIn("monitor_path", row)

    def test_prunes_expired_and_oversized_history(self):
        current = datetime.now(timezone.utc)
        expired = {"event": "guard_state", "timestamp": (current - timedelta(days=31)).isoformat(),
                   "setting": "physical_window_guard_enabled", "enabled": False}
        recent = {"event": "guard_state", "timestamp": current.isoformat(),
                  "setting": "physical_window_guard_enabled", "enabled": True}
        self.path.write_text(json.dumps(expired) + "\n" + json.dumps(recent) + "\n", encoding="utf-8")
        self.store._retention_clock = lambda: current + timedelta(seconds=1)
        events = self.store.read()["events"]
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["enabled"])
        self.assertLessEqual(self.path.stat().st_size, 1_048_576)

    def test_clear_removes_history_without_creating_other_files(self):
        self.store.append({"event": "manager_state", "result": "started"})
        self.assertEqual(self.store.clear()["event_count"], 0)
        self.assertEqual(self.store.read()["events"], [])
        self.assertEqual([p.name for p in self.path.parent.iterdir()], [self.path.name])

    def test_module_loads_and_reloads_through_versioned_host(self):
        host = ModuleHost(ROOT)
        info = host.load("display_diagnostics")
        host.dispose_all()
        import shutil
        scratch = Path(self.temp.name) / "candidate"
        shutil.copytree(ROOT / "core", scratch / "core")
        shutil.copytree(ROOT / "modules" / "display_diagnostics", scratch / "modules" / "display_diagnostics")
        manifest = json.loads((ROOT / "module-registry.json").read_text(encoding="utf-8"))
        manifest["modules"] = [x for x in manifest["modules"] if x["id"] == "display_diagnostics"]
        (scratch / "module-registry.json").write_text(json.dumps(manifest), encoding="utf-8")
        module_path = scratch / "modules" / "display_diagnostics" / "module.py"
        source = module_path.read_text(encoding="utf-8")
        source = source.replace(
            "def create(context=None):\n    return DisplayDiagnosticsModule((context or {}).get(\"store_factory\"))",
            "def create(context=None):\n    return DisplayDiagnosticsModule(lambda: DisplayDiagnosticStore(" + repr(str(self.path)) + "))"
        )
        module_path.write_text(source, encoding="utf-8")
        host = ModuleHost(scratch)
        info = host.load("display_diagnostics")
        first = host.invoke("display_diagnostics", "record", {"event": "guard_state",
            "setting": "physical_window_guard_enabled", "enabled": False})
        self.assertTrue(first["stored"])
        second = host.reload("display_diagnostics")
        self.assertEqual(second["generation"], info["generation"] + 1)
        self.assertEqual(host.invoke("display_diagnostics", "read")["event_count"], 1)
        host.dispose_all()


if __name__ == "__main__":
    unittest.main()
