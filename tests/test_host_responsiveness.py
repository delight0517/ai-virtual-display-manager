"""Host concurrency checks using blocked synthetic modules, never devices."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace

from core.module_host import ModuleHost


class HostResponsivenessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "module-registry.json").write_text(json.dumps({"modules": []}))
        self.host = ModuleHost(root)
        self.entered = threading.Event()
        self.unblock = threading.Event()
        self.threads = []
        self.refresh_errors = []
        self.disposed = False

        def refresh():
            self.entered.set()
            if not self.unblock.wait(3):
                raise RuntimeError("Fixture was not released")
            return {"ok": True}

        def dispose():
            self.disposed = True

        self.instance = SimpleNamespace(refresh=refresh, snapshot=lambda: {},
            dispose=dispose, action=lambda *_: {}, command=lambda *_: {},
            device_action=lambda *_: {})
        self.host._loaded["fake"] = SimpleNamespace(module_id="fake",
            module=SimpleNamespace(ACTIONS=("status",), __name__="_fixture"),
            instance=self.instance)

    def tearDown(self):
        self.unblock.set()
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(), "Fixture thread leaked")
        self.host.active_operations = 0  # also clean up deliberately failed baseline runs
        self.host.dispose_all()
        self.temp.cleanup()

    def start_refresh(self):
        def work():
            try:
                self.host.refresh("fake")
            except Exception as error:
                self.refresh_errors.append(error)
        thread = threading.Thread(target=work, daemon=True)
        self.threads.append(thread)
        thread.start()
        self.assertTrue(self.entered.wait(1))

    def assert_rejected_promptly(self, call):
        outcome = []
        def work():
            try:
                call()
                outcome.append(None)
            except Exception as error:
                outcome.append(error)
        thread = threading.Thread(target=work, daemon=True)
        self.threads.append(thread)
        thread.start()
        thread.join(.25)
        self.assertFalse(thread.is_alive(), "UI call waited on the slow device refresh")
        self.assertIsInstance(outcome[0], RuntimeError)

    def test_slow_refresh_reserves_operation(self):
        self.start_refresh()
        self.assertEqual(self.host.active_operations, 1)

    def test_ui_invoke_rejects_without_waiting_for_device(self):
        self.start_refresh()
        self.assert_rejected_promptly(lambda: self.host.invoke("fake", "status"))

    def test_lifecycle_and_snapshot_cannot_race_refresh(self):
        self.start_refresh()
        for name, call in [
            ("snapshot", lambda: self.host.snapshot("fake")),
            ("reload", lambda: self.host.reload("fake")),
            ("load", lambda: self.host.load("other")),
            ("dispose", self.host.dispose_all),
        ]:
            with self.subTest(operation=name):
                self.assert_rejected_promptly(call)
        self.assertFalse(self.disposed)
        self.assertIs(self.host._loaded["fake"].instance, self.instance)

    def test_overlapping_operations_are_rejected_promptly(self):
        self.start_refresh()
        for name, call in [
            ("refresh", lambda: self.host.refresh("fake")),
            ("command", lambda: self.host.command("fake", "PING")),
            ("device_action", lambda: self.host.device_action("fake", "status")),
        ]:
            with self.subTest(operation=name):
                self.assert_rejected_promptly(call)

    def test_failed_refresh_releases_reservation(self):
        def fail():
            raise OSError("synthetic device error")
        self.instance.refresh = fail
        with self.assertRaisesRegex(OSError, "synthetic device error"):
            self.host.refresh("fake")
        self.assertEqual(self.host.active_operations, 0)
        self.assertEqual(self.host.invoke("fake", "status"), {})

    def test_action_can_read_topology_in_same_thread_without_releasing_ownership(self):
        counts = []
        def refresh():
            counts.append(self.host.active_operations)
            # Other UI threads must still fail while a pool action performs
            # its synchronous injected topology reader.
            self.assert_rejected_promptly(lambda: self.host.invoke("fake", "status"))
            return {"ok": True}
        self.instance.refresh = refresh
        self.instance.action = lambda *_: self.host.refresh("fake")
        self.assertEqual(self.host.invoke("fake", "status"), {"ok": True})
        self.assertEqual(counts, [2])
        self.assertEqual(self.host.active_operations, 0)
        self.assertIsNone(self.host._operation_thread_id)

    def test_nested_refresh_failure_preserves_outer_operation_guard(self):
        def refresh():
            raise OSError("nested read failed")
        def action(*_):
            with self.assertRaisesRegex(OSError, "nested read failed"):
                self.host.refresh("fake")
            self.assertEqual(self.host.active_operations, 1)
            self.assert_rejected_promptly(lambda: self.host.invoke("fake", "status"))
        self.instance.refresh = refresh
        self.instance.action = action
        self.host.invoke("fake", "status")
        self.assertEqual(self.host.active_operations, 0)
        self.assertIsNone(self.host._operation_thread_id)

    def test_completed_refresh_allows_subsequent_action_and_disposal(self):
        self.start_refresh()
        self.unblock.set()
        self.threads[0].join(1)
        self.assertFalse(self.threads[0].is_alive())
        self.assertEqual(self.refresh_errors, [])
        self.assertEqual(self.host.active_operations, 0)
        self.assertEqual(self.host.invoke("fake", "status"), {})
        self.host.dispose_all()
        self.assertTrue(self.disposed)


if __name__ == "__main__":
    unittest.main()
