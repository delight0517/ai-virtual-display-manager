import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from modules.pomodoro_task_target.module import build_target, require_stopped_instance
from modules.task_session_runtime import module as runtime


class PomodoroTargetTests(unittest.TestCase):
    def test_profile_busy_or_status_failure_prevents_launch(self):
        self.assertEqual(require_stopped_instance(lambda: [12]), "POMODORO_ALREADY_RUNNING")
        self.assertEqual(require_stopped_instance(lambda: None), "POMODORO_PROCESS_STATUS_UNAVAILABLE")
        self.assertIsNone(require_stopped_instance(lambda: []))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.exe = self.root / "versions" / "verified" / "vintage_pomodoro.exe"
        self.exe.parent.mkdir(parents=True)
        self.exe.write_bytes(b"synthetic exe")
        self.engine = self.exe.parent / "data" / "app.so"
        self.engine.parent.mkdir()
        self.engine.write_bytes(b"synthetic engine")
        self.record = {"id": "vintage-pomodoro", "windows": {
            "installed": str(self.root), "activeExecutable": str(self.exe),
            "installedVerified": True,
            "installedExeSha256": hashlib.sha256(self.exe.read_bytes()).hexdigest(),
            "installedAppSoSha256": hashlib.sha256(self.engine.read_bytes()).hexdigest()}}
        self.registry = self.root / "registry.json"
        (self.root / "update-ready.json").write_text(json.dumps({
            "buildId": "verified", "files": [
                {"path": "vintage_pomodoro.exe", "sha256": self.record["windows"]["installedExeSha256"]},
                {"path": "data/app.so", "sha256": self.record["windows"]["installedAppSoSha256"]}]}))

    def build(self):
        self.registry.write_text(json.dumps({"apps": [self.record]}))
        return build_target(runtime, self.registry)

    def test_private_target_has_no_host_outputs_or_arbitrary_actions(self):
        target, error = self.build()
        self.assertIsNone(error)
        self.assertEqual(target.app_id, "pomodoro")
        self.assertEqual(target.isolation_mode, "private_desktop")
        self.assertEqual(target.maximum_output_count, 0)
        self.assertFalse(target.action_ids)

    def test_engine_tamper_is_rejected(self):
        self.engine.write_bytes(b"unverified replacement")
        self.assertEqual(self.build()[1], "POMODORO_ENGINE_HASH_MISMATCH")

    def test_exe_tamper_is_rejected(self):
        self.exe.write_bytes(b"unverified replacement")
        self.assertEqual(self.build()[1], "POMODORO_IMAGE_HASH_MISMATCH")

    def test_unattested_install_is_rejected(self):
        self.record["windows"]["installedVerified"] = False
        self.assertEqual(self.build()[1], "POMODORO_INSTALL_NOT_VERIFIED")
