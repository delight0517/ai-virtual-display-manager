from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.task_display_lease.authority import TaskDisplayLeaseAuthority


RUN = "c9071396-0607-44b4-b8b1-922f00523f60"
CLIENT = "a8d47479-df8b-496e-9f8d-f484d340b4c4"
INSTANCE = "696236b53e7540ebac6c853b8e0d62e3"
LEASE = "ab5cf196-70ec-414b-b510-cce2de0d54d8"


def nonce(index):
    return base64.urlsafe_b64encode(bytes([index]) * 32).decode("ascii").rstrip("=")


def endpoint():
    return {"display_name": "\\\\.\\DISPLAY10", "monitor_device_id": "DISPLAY\\MTT1337\\0000",
            "monitor_path": "\\\\?\\DISPLAY#MTT1337#0000#{GUID}",
            "monitor_parent": "ROOT\\DISPLAY\\0000", "attached": True,
            "virtual": True, "monitor_identity_verified": True,
            "topology_verified": True, "verified": True, "cloned": False,
            "clone_source_count": 1, "width": 1920, "height": 1080}


class TaskDisplayLeaseAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.topology = {"topology_verified": True, "mtt_device_instance_id": "ROOT\\DISPLAY\\0000",
                         "displays": [endpoint()]}
        self.heartbeats = []
        self.releases = []
        self.authority = TaskDisplayLeaseAuthority(
            consumer_id="enderman-qa-workbench", instance_id=INSTANCE,
            peer_authorized=lambda pid: pid == 4321,
            pool_eligible_endpoints=lambda _topology: {
                "\\\\.\\DISPLAY10\nDISPLAY\\MTT1337\\0000\n\\\\?\\DISPLAY#MTT1337#0000#{GUID}"},
            topology_reader=lambda: self.topology,
            heartbeat=self.heartbeats.append, release_pool_lease=self.releases.append,
            monotonic=lambda: self.now,
            utc_now=lambda: "2026-10-30T14:00:00Z", id_factory=lambda: LEASE)

    def acquire(self, *, pid=4321, rid="b103fb87-3030-43e2-9f77-ece8e46c02d5", nonce_value=None):
        return self.authority.handle({"version": 2, "request_id": rid,
            "nonce": nonce_value or nonce(1), "action": "acquire",
            "consumer_id": "enderman-qa-workbench", "run_id": RUN,
            "workbench_pid": pid, "client_session_id": CLIENT, "instance_id": INSTANCE,
            "requested_scope": ["launch-client", "client-input", "framebuffer-capture"],
            "requested_ttl_seconds": 120}, pid)

    def operation(self, action, *, rid, n, lease_id=LEASE):
        return self.authority.handle({"version": 2, "request_id": rid, "nonce": nonce(n),
            "action": action, "consumer_id": "enderman-qa-workbench", "run_id": RUN,
            "workbench_pid": 4321, "client_session_id": CLIENT,
            "instance_id": INSTANCE, "lease_id": lease_id}, 4321)

    def test_issues_bounded_lease_and_supports_validate_renew_release(self):
        reply = self.acquire()
        self.assertEqual(reply["state"], "acquired")
        self.assertEqual(reply["display"]["active"], True)
        self.assertEqual(reply["display"]["mirrored"], False)
        self.assertEqual(reply["display"]["visible_on_physical_output"], False)
        self.assertEqual(reply["expires_at_utc"], "2026-10-30T14:02:00Z")
        self.assertEqual(self.heartbeats, [LEASE])
        self.assertEqual(self.operation("validate", rid="49d92d8b-9896-4ff8-a7f4-590f3bb9b201", n=2)["state"], "valid")
        self.assertEqual(self.operation("renew", rid="49d92d8b-9896-4ff8-a7f4-590f3bb9b202", n=3)["state"], "valid")
        self.assertEqual(self.operation("release", rid="49d92d8b-9896-4ff8-a7f4-590f3bb9b203", n=4)["state"], "released")
        self.assertEqual(self.operation("release", rid="49d92d8b-9896-4ff8-a7f4-590f3bb9b205", n=5)["state"], "released")
        self.assertEqual(self.releases, [LEASE])

    def test_rejects_wrong_peer_physical_clone_and_topology_change(self):
        self.assertEqual(self.acquire(pid=9999)["error_code"], "CONSUMER_PROCESS_UNAUTHORIZED")
        self.topology["displays"][0]["clone_source_count"] = 2
        self.topology["displays"][0]["cloned"] = True
        self.assertEqual(self.acquire(rid="c9011396-0607-44b4-b8b1-922f00523f60", nonce_value=nonce(2))["error_code"],
                         "NO_ACTIVE_UNMIRRORED_OUTPUT")
        self.topology["displays"][0] = endpoint()
        self.acquire(rid="c9021396-0607-44b4-b8b1-922f00523f60", nonce_value=nonce(3))
        self.topology["displays"][0]["monitor_path"] = "\\\\?\\DISPLAY#OTHER#0000#{GUID}"
        changed = self.operation("validate", rid="c9031396-0607-44b4-b8b1-922f00523f60", n=4)
        self.assertEqual(changed["error_code"], "TOPOLOGY_CHANGED")
        self.assertEqual(self.releases, [LEASE])

    def test_rejects_replay_and_wrong_lease(self):
        first = self.acquire()
        self.assertEqual(first["state"], "acquired")
        replay = self.acquire()
        self.assertEqual(replay["error_code"], "REQUEST_REPLAYED")
        wrong = self.operation("release", rid="49d92d8b-9896-4ff8-a7f4-590f3bb9b204", n=5,
                               lease_id="00000000-0000-4000-8000-000000000000")
        self.assertEqual(wrong["error_code"], "LEASE_NOT_FOUND")

    def test_rejects_invalid_scope_and_ttl(self):
        base = {"version": 2, "request_id": "b103fb87-3030-43e2-9f77-ece8e46c02d5",
            "nonce": nonce(5), "action": "acquire", "consumer_id": "enderman-qa-workbench",
            "run_id": RUN, "workbench_pid": 4321, "client_session_id": CLIENT,
            "instance_id": INSTANCE, "requested_scope": ["read-user-files"],
            "requested_ttl_seconds": 120}
        self.assertEqual(self.authority.handle(base, 4321)["error_code"], "SCOPE_INVALID")
        base["request_id"] = "c103fb87-3030-43e2-9f77-ece8e46c02d5"
        base["nonce"] = nonce(6)
        base["requested_scope"] = ["framebuffer-capture"]
        base["requested_ttl_seconds"] = 121
        self.assertEqual(self.authority.handle(base, 4321)["error_code"], "TTL_INVALID")


if __name__ == "__main__":
    unittest.main(verbosity=2)
