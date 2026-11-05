import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from core.module_host import ModuleHost
from app import bind_task_runtime_dependencies
from modules.router_task_target.module import (
    PRIVATE_PROFILE_TOKEN,
    ROUTER_WINDOW_CLASS_PREFIX,
    build_target,
)


ROOT = Path(__file__).resolve().parents[1]


class RouterPrivateTargetCompositionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vdm-router-target-")
        self.root = Path(self.temp.name)
        self.image = self.root / "qa-candidate" / "AI Account Router.exe"
        self.image.parent.mkdir(parents=True)
        self.image.write_bytes(b"synthetic router executable")
        self.image_hash = hashlib.sha256(self.image.read_bytes()).hexdigest()
        self.normal_image = self.root / "installed" / "AI Account Router.exe"
        self.registry_path = self.root / "APP_PATH_REGISTRY.json"
        self.record = {
            "id": "ai-account-router",
            "version": "1.3.0",
            "windows": {
                "source": str(self.root),
                "target": str(self.normal_image),
                "candidateNativePath": str(self.normal_image),
                "candidateNativeSha256": "0" * 64,
                "privateQaNativePath": str(self.image),
                "privateQaNativeSha256": self.image_hash,
                "privateQaModeSupported": True,
                "privateQaNativeVerified": True,
            },
        }
        self.write_registry()
        self.host = ModuleHost(ROOT)
        self.host.load("task_session_runtime")
        self.loaded = self.host._loaded["task_session_runtime"]

    def tearDown(self):
        self.host.dispose_all()
        self.temp.cleanup()

    def write_registry(self):
        self.registry_path.write_text(json.dumps({"apps": [self.record]}), encoding="utf-8")

    def test_builds_only_the_exact_zero_output_private_target(self):
        target, error = build_target(self.loaded.module, self.registry_path)
        self.assertIsNone(error)
        self.assertEqual(target.app_id, "ai_account_router_qa")
        self.assertEqual(target.launch.executable_path, str(self.image.resolve()))
        self.assertNotEqual(target.launch.executable_path,
                            self.record["windows"]["target"])
        self.assertNotEqual(target.launch.executable_path,
                            self.record["windows"]["candidateNativePath"])
        self.assertEqual(target.launch.executable_sha256, self.image_hash)
        self.assertEqual(target.launch.arguments,
                         (f"--qa-private-profile={PRIVATE_PROFILE_TOKEN}",))
        self.assertEqual(target.launch.window_class_names,
                         (ROUTER_WINDOW_CLASS_PREFIX + "*",))
        self.assertEqual(target.action_ids, frozenset())
        self.assertEqual(target.isolation_mode, "private_desktop")
        self.assertEqual(target.minimum_output_count, 0)
        self.assertEqual(target.maximum_output_count, 0)
        self.assertEqual(target.output_profiles, frozenset({"private"}))
        self.assertFalse(target.requires_output_lease)

    def test_rejects_unattested_current_installed_image(self):
        self.record["windows"]["privateQaNativeVerified"] = False
        self.write_registry()
        target, error = build_target(self.loaded.module, self.registry_path)
        self.assertIsNone(target)
        self.assertEqual(error, "ROUTER_PRIVATE_QA_NOT_ATTESTED")

    def test_rejects_hash_mismatch(self):
        self.image.write_bytes(b"changed after registry attestation")
        target, error = build_target(self.loaded.module, self.registry_path)
        self.assertIsNone(target)
        self.assertEqual(error, "ROUTER_PRIVATE_QA_IMAGE_MISMATCH")

    def test_rejects_private_candidate_outside_registered_source_root(self):
        with tempfile.TemporaryDirectory(prefix="vdm-router-outside-") as other:
            outside_image = Path(other) / "AI Account Router.exe"
            outside_image.write_bytes(b"outside synthetic router executable")
            self.record["windows"]["privateQaNativePath"] = str(outside_image)
            self.record["windows"]["privateQaNativeSha256"] = hashlib.sha256(
                outside_image.read_bytes()).hexdigest()
            self.write_registry()
            target, error = build_target(self.loaded.module, self.registry_path)
        self.assertIsNone(target)
        self.assertEqual(error, "ROUTER_PRIVATE_QA_IMAGE_MISMATCH")

    def test_rejects_ambiguous_router_records(self):
        self.write_registry()
        self.record["name"] = "duplicate"
        self.write_registry()
        payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        payload["apps"].append(self.record)
        self.registry_path.write_text(json.dumps(payload), encoding="utf-8")
        target, error = build_target(self.loaded.module, self.registry_path)
        self.assertIsNone(target)
        self.assertEqual(error, "ROUTER_REGISTRY_AMBIGUOUS")

    def test_app_composes_attested_router_without_starting_a_target(self):
        self.host.load("virtual_output_pool")
        self.host.load("only_chat_task_runtime")
        broker_source = self.host._loaded["only_chat_task_runtime"]
        broker_source.module.APP_PATH_REGISTRY = self.registry_path

        class FakeBroker:
            runtime = None

            def bind_runtime(self, runtime):
                self.runtime = runtime

        broker = FakeBroker()
        coordinator = bind_task_runtime_dependencies(
            self.loaded,
            self.host._loaded["virtual_output_pool"].instance,
            lambda: {},
            broker,
            only_chat_target_source=broker_source,
        )

        runtime = self.loaded.instance
        state = runtime.refresh()
        target = runtime._targets["ai_account_router_qa"]
        self.assertIs(broker.runtime, runtime)
        self.assertIsInstance(
            coordinator,
            self.loaded.module.TaskOutputLeaseCoordinator,
        )
        self.assertEqual(state["target_count"], 1)
        self.assertEqual(state["active_session_id"], None)
        self.assertEqual(target.isolation_mode, "private_desktop")
        self.assertEqual(target.maximum_output_count, 0)


if __name__ == "__main__":
    unittest.main()
