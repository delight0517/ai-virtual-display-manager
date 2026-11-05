from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.task_display_lease.replay_store import ReplayNonceStore, ReplayStoreError


class ReplayNonceStoreTests(unittest.TestCase):
    def test_replay_is_rejected_after_reconstructing_store(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "replay.json"
            first = ReplayNonceStore(path, clock=lambda: 10)
            self.assertTrue(first.consume("request", "nonce"))
            self.assertFalse(ReplayNonceStore(path, clock=lambda: 11).consume("request", "nonce"))
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("request", path.read_text(encoding="utf-8"))
            self.assertEqual(len(stored["entries"]), 1)

    def test_expired_entries_are_pruned_and_invalid_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "replay.json"
            ReplayNonceStore(path, clock=lambda: 1).consume("old", "nonce")
            self.assertTrue(ReplayNonceStore(path, clock=lambda: 90000).consume("new", "nonce"))
            path.write_text("not-json", encoding="utf-8")
            with self.assertRaisesRegex(ReplayStoreError, "REPLAY_STATE_UNREADABLE"):
                ReplayNonceStore(path).consume("any", "nonce")


if __name__ == "__main__":
    unittest.main(verbosity=2)
