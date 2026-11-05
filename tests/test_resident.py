import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from core.resident import ResidentController


class Registration:
    def __init__(self): self.value = None
    def read(self): return self.value
    def write(self, value): self.value = value
    def remove(self): self.value = None


class ResidentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.reg = Registration()
        self.now = 1000
        self.path = Path(self.tmp.name) / "resident.json"
        self.controller = ResidentController(Path(self.tmp.name), "pythonw.exe", self.path,
                                             self.reg, lambda: self.now)

    def test_exact_registration_enable_disable(self):
        self.assertFalse(self.controller.startup_allowed())
        self.assertTrue(self.controller.enable()["startup_enabled"])
        self.assertIn("--startup", self.reg.value)
        self.assertFalse(self.controller.disable()["startup_enabled"])

    def test_source_dispatcher_command_remains_exact(self):
        self.assertEqual(self.controller.startup_argv(), [
            str(self.controller.pythonw), "-B",
            str(self.controller.root / "app.py"), "--startup"])
        self.assertEqual(self.controller.command,
                         subprocess.list2cmdline(self.controller.startup_argv()))

    def test_frozen_dispatcher_uses_its_exact_executable_for_registration_and_recovery(self):
        executable = Path(self.tmp.name) / "AI Virtual Display Manager.exe"
        packed = ResidentController(self.tmp.name, "pythonw.exe", self.path, self.reg,
                                    lambda: self.now, frozen_executable=executable)
        expected = [str(executable.resolve()), "--startup"]
        self.assertEqual(packed.startup_argv(), expected)
        self.assertEqual(packed.command, subprocess.list2cmdline(expected))
        self.reg.write("other owner")
        self.assertFalse(packed.startup_allowed())
        self.reg.write(packed.command)
        self.assertTrue(packed.startup_allowed())
        packed.pause("frozen-task")
        self.assertEqual(json.loads(self.path.read_text())["pause"]["registration"], packed.command)
        self.assertEqual(packed.startup_argv(), expected)

    def test_pause_is_durable_and_expires_after_sleep(self):
        self.controller.enable()
        expected = self.reg.value
        self.controller.pause("task-1")
        self.assertFalse(self.controller.startup_allowed())
        restarted = ResidentController(Path(self.tmp.name), "pythonw.exe", self.path,
                                       self.reg, lambda: self.now)
        self.assertTrue(restarted.status()["developer_paused"])
        self.assertEqual(self.reg.value, expected)
        self.now += 10801
        self.assertTrue(restarted.startup_allowed())
        self.assertFalse(restarted.status()["developer_paused"])

    def test_expiry_preserves_disabled_prior_state(self):
        self.controller.pause("task-2", 10)
        self.now += 11
        self.assertFalse(self.controller.startup_allowed())

    def test_stop_during_pause_never_reenables_startup(self):
        self.controller.enable()
        self.controller.pause("task-3", 10)
        self.controller.disable()
        self.now += 11
        self.assertFalse(self.controller.startup_allowed())

    def test_external_changes_preserved_and_recovery_failure_visible(self):
        self.controller.enable()
        self.controller.pause("task-4", 10)
        self.reg.value = "external owner"
        self.now += 11
        for action in (self.controller.status, self.controller.resume, self.controller.disable):
            with self.assertRaises(RuntimeError): action()
        self.assertEqual(self.reg.value, "external owner")
        self.assertIsNotNone(json.loads(self.path.read_text())["pause"])

    def test_corrupt_state_fails_closed(self):
        self.controller.enable()
        self.path.write_text('{"schema":1,"pause":{}}')
        with self.assertRaises(RuntimeError): self.controller.startup_allowed()

    def test_resume_restores_exact_prior_gate(self):
        self.controller.enable()
        self.controller.pause("task-5")
        self.assertTrue(self.controller.resume()["startup_enabled"])
        self.assertTrue(self.controller.startup_allowed())
