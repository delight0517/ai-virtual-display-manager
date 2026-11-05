from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.virtual_output_pool.state_store import VirtualOutputStateStore


class VirtualOutputStateStoreTests(unittest.TestCase):
    def test_atomic_round_trip_and_clear(self):
        with tempfile.TemporaryDirectory() as temp:
            store = VirtualOutputStateStore(Path(temp) / "session.json")
            value = {"schema_version": 1, "saved_at_epoch": 10.0,
                     "session": {"phase": "active"}}
            self.assertIsNone(store.load())
            store.save(value)
            self.assertEqual(store.load(), value)
            store.clear()
            self.assertIsNone(store.load())

    def test_oversize_or_non_object_journal_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "session.json"
            store = VirtualOutputStateStore(path)
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must be an object"):
                store.load()
            path.write_text("x" * (store.MAX_BYTES + 1), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "size limit"):
                store.load()


if __name__ == "__main__":
    unittest.main(verbosity=2)
