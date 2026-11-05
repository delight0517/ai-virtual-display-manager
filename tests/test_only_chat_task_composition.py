import hashlib
import json
import tempfile
import types
import unittest
from pathlib import Path

from app import build_only_chat_qa_target, task_runtime_broker_summary
from core.module_host import ModuleHost
from modules.task_session_runtime.isolated_target_backend import (
    DescriptorPrivateDesktopBackend,
    TASK_PROFILE_TOKEN,
)


ROOT = Path(__file__).resolve().parents[1]


class _Session:
    def __init__(self, descriptor):
        self.descriptor = descriptor
        self.arguments = descriptor.arguments

    def identity(self):
        return {
            "process_id": 101,
            "window_id": 202,
            "job_owned": True,
            "window_absent_from_input_desktop": True,
            "input_desktop_unchanged": True,
            "executable_path": self.descriptor.executable_path,
            "executable_sha256": self.descriptor.executable_sha256,
        }

    def stop(self, *, timeout_seconds):
        assert timeout_seconds > 0
        return {"stopped": True, "target_stopped": True, "job_closed": True,
                "desktop_removed": True, "input_desktop_unchanged": True}


class OnlyChatTaskCompositionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aivdm-onlychat-composition-")
        self.root = Path(self.temp.name)
        self.install = self.root / "install"
        resources = self.install / "resources"
        resources.mkdir(parents=True)
        self.exe = self.install / "Only Chat.exe"
        self.exe.write_bytes(b"synthetic executable image")
        self.archive = resources / "app.asar"
        self.archive.write_bytes(b"verified isolated profile contract")
        self.registry_path = self.root / "APP_PATH_REGISTRY.json"
        self.expected_archive_hash = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.registry_path.write_text(json.dumps({
            "apps": [{"id": "only-chat-windows", "name": "Only Chat",
                      "windows": {"installed": str(self.install),
                                  "installedRuntimeVerified": True,
                                  "installedArchiveHash": self.expected_archive_hash}}]
        }), encoding="utf-8")
        self.broker_module = types.SimpleNamespace(
            APP_PATH_REGISTRY=self.registry_path,
            registered_only_chat_exe_path=lambda _path: self.exe,
        )
        self.host = ModuleHost(ROOT)
        self.info = self.host.load("task_session_runtime")
        self.loaded = self.host._loaded["task_session_runtime"]
        self.runtime_module = self.loaded.module

    def tearDown(self):
        self.host.dispose_all()
        self.temp.cleanup()

    def test_target_requires_exact_installed_archive_attestation(self):
        target, error = build_only_chat_qa_target(
            self.runtime_module, self.broker_module)
        self.assertIsNone(error)
        self.assertEqual(target.app_id, "only_chat_qa")
        self.assertEqual(target.isolation_mode, "private_desktop")
        self.assertEqual(target.minimum_output_count, 0)
        self.assertFalse(target.requires_output_lease)
        self.assertEqual(target.launch.executable_path, str(self.exe.resolve()))
        self.assertEqual(target.launch.action_ids, frozenset())
        self.assertIn("--only-chat-isolated-qa", target.launch.arguments)
        self.assertNotIn("--only-chat-qa-background", target.launch.arguments)
        self.assertTrue(any(TASK_PROFILE_TOKEN in arg for arg in target.launch.arguments))

    def test_archive_mismatch_rejects_the_target(self):
        registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
        registry["apps"][0]["windows"]["installedArchiveHash"] = "0" * 64
        self.registry_path.write_text(json.dumps(registry), encoding="utf-8")
        target, error = build_only_chat_qa_target(
            self.runtime_module, self.broker_module)
        self.assertIsNone(target)
        self.assertEqual(error, "ONLY_CHAT_RUNTIME_IDENTITY_MISMATCH")

    def test_status_summary_surfaces_readiness_identity_error(self):
        summary = task_runtime_broker_summary({
            "pipe_ready": True,
            "backend_available": True,
            "target_count": 0,
            "readiness_error": "ONLY_CHAT_RUNTIME_IDENTITY_MISMATCH",
            "authenticated_request_count": 0,
        })
        self.assertIn("ONLY_CHAT_RUNTIME_IDENTITY_MISMATCH", summary)
        self.assertIn("앱 실행은 거부됩니다", summary)

    def test_status_summary_does_not_report_ready_when_pool_is_missing(self):
        summary = task_runtime_broker_summary({
            "pipe_ready": True,
            "backend_available": True,
            "target_count": 1,
            "output_lease_required": True,
            "output_lease_bound": True,
            "readiness_error": "OUTPUT_POOL_NOT_READY",
            "authenticated_request_count": 0,
        })
        self.assertIn("OUTPUT_POOL_NOT_READY", summary)
        self.assertIn("앱 실행을 거부합니다", summary)

    def test_hash_loaded_runtime_and_backend_share_contract_types(self):
        target, error = build_only_chat_qa_target(
            self.runtime_module, self.broker_module)
        self.assertIsNone(error)
        temp_root = self.root / "system-temp"
        temp_root.mkdir()
        observed = {}

        def create_session(spec, _session_id, *, timeout_seconds):
            observed["arguments"] = spec.launch.arguments
            observed["profile"] = Path(next(
                arg.split("=", 1)[1] for arg in spec.launch.arguments
                if arg.startswith("--only-chat-qa-profile=")))
            assert timeout_seconds > 0
            return _Session(spec.launch)

        backend = DescriptorPrivateDesktopBackend(
            create_session, runtime_types=self.runtime_module, temp_root=temp_root)
        runtime = self.loaded.instance
        runtime.configure_backend(backend, {target.app_id: target})
        status = runtime.refresh()
        self.assertTrue(status["backend_available"])
        self.assertEqual(status["target_count"], 1)
        self.assertIsInstance(self.loaded.instance, self.runtime_module.TaskSessionRuntime)
        handle = backend.start(
            target, "onlychat-qa-session",
            output_request=self.runtime_module.OutputRequest(2, "high"),
            timeout_seconds=5,
        )
        self.assertIsInstance(handle, self.runtime_module.TargetHandle)
        self.assertTrue(observed["profile"].is_dir())
        self.assertEqual(observed["profile"].parent, temp_root.resolve())
        report = next(arg for arg in observed["arguments"]
                      if arg.startswith("--only-chat-qa-startup-report="))
        self.assertIn(str(observed["profile"]), report)
        receipt = backend.stop(handle, timeout_seconds=5)
        self.assertIsInstance(receipt, self.runtime_module.StopReceipt)
        self.assertTrue(receipt.verified)
        self.assertTrue(receipt.profile_data_removed)
        self.assertFalse(observed["profile"].exists())

    def test_empty_registry_with_identity_error_remains_fail_closed(self):
        runtime = self.runtime_module.TaskSessionRuntime(watchdog_interval=0)
        runtime.init()
        try:
            backend = DescriptorPrivateDesktopBackend(
                lambda *_a, **_k: self.fail("No target may launch"),
                runtime_types=self.runtime_module, temp_root=self.root)
            runtime.configure_backend(
                backend, {}, target_registry_error="ONLY_CHAT_RUNTIME_IDENTITY_MISMATCH")
            state = runtime.refresh()
            self.assertTrue(state["backend_available"])
            self.assertEqual(state["target_count"], 0)
            self.assertEqual(state["readiness_error"],
                             "ONLY_CHAT_RUNTIME_IDENTITY_MISMATCH")
        finally:
            runtime.dispose()

    def test_canonical_task_runtime_module_version_is_registered(self):
        self.assertEqual(self.info["version"], "0.1.21")


if __name__ == "__main__":
    unittest.main()
