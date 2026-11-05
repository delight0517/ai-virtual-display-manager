from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.task_display_lease.module import (
    TaskDisplayLeaseModule, registered_workbench_exe,
)
from modules.task_display_lease.replay_store import ReplayNonceStore


class FakeServer:
    def __init__(self, handler, **kwargs):
        self.handler = handler
        self.options = kwargs
        self.ready = False
        self.active_clients = 0

    def start(self):
        self.ready = True

    def stop(self):
        self.ready = False


class TaskDisplayLeaseModuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.exe = self.root / "EndermanQa.Desktop.exe"
        self.exe.write_bytes(b"synthetic executable identity fixture")
        digest = hashlib.sha256(self.exe.read_bytes()).hexdigest().upper()
        self.registry = self.root / "registry.json"
        self.registry.write_text(json.dumps({"apps": [{
            "id": "enderman-qa-workbench", "verified": True,
            "windows": {"installedVerified": True,
                        "installed": str(self.root), "installedExeSha256": digest}}]}),
            encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_registry_path_must_match_verified_installed_hash(self):
        self.assertEqual(registered_workbench_exe(self.registry), self.exe)
        data = json.loads(self.registry.read_text(encoding="utf-8"))
        data["apps"][0]["windows"]["installedExeSha256"] = "0" * 64
        self.registry.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(registered_workbench_exe(self.registry))

    def test_hidden_self_check_never_binds_global_pipe(self):
        created = []
        module = TaskDisplayLeaseModule(
            registry_path=self.registry, server_factory=lambda *a, **k: created.append(True),
            replay_store_factory=lambda: ReplayNonceStore(self.root / "replay.json"))
        with patch.object(sys, "argv", ["app.py", "--self-check"]):
            module.init()
        self.assertEqual(created, [])
        self.assertEqual(module.refresh()["error_code"], "BROKER_NOT_STARTED_IN_SELF_CHECK")
        module.dispose()

    def test_normal_init_uses_authenticated_peer_dispatch_and_is_not_reloadable(self):
        servers = []
        def factory(handler, **kwargs):
            server = FakeServer(handler, **kwargs)
            servers.append(server)
            return server
        module = TaskDisplayLeaseModule(
            registry_path=self.registry, server_factory=factory,
            process_image_checker=lambda pid, path: pid == 77 and path == self.exe,
            replay_store_factory=lambda: ReplayNonceStore(self.root / "replay.json"))
        with patch.object(sys, "argv", ["app.py"]):
            module.init()
        self.assertEqual(len(servers), 1)
        self.assertIsNotNone(servers[0].options.get("authenticated_handler"))
        self.assertIsNotNone(servers[0].options.get("client_authenticator"))
        self.assertFalse(module.refresh()["provider_available"])
        self.assertEqual(module.refresh()["error_code"], "DEPENDENCIES_NOT_BOUND")
        reader = lambda: {"topology_verified": True}
        module.bind_dependencies(pool=object(), topology_reader=reader)
        self.assertIs(module._authority.topology_reader, reader)
        self.assertTrue(module.refresh()["provider_available"])
        with self.assertRaisesRegex(RuntimeError, "process-global lease pipe"):
            module.snapshot()
        module.dispose()


if __name__ == "__main__":
    unittest.main(verbosity=2)
