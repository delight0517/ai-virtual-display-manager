from __future__ import annotations

import unittest
from unittest.mock import Mock

from core.mtt_status_cache import get_status, invalidate


class MttStatusCacheTests(unittest.TestCase):
    def setUp(self):
        invalidate()

    def tearDown(self):
        invalidate()

    def test_reuses_status_and_returns_defensive_copies(self):
        loader = Mock(return_value={"device": {"status": "Started"}})
        first, observed, hit = get_status(loader)
        first["device"]["status"] = "changed"
        second, same_observed, hit = get_status(loader)
        self.assertEqual(second["device"]["status"], "Started")
        self.assertEqual(observed, same_observed)
        self.assertTrue(hit)
        loader.assert_called_once()

    def test_expiry_and_force_refresh_bypass_cache(self):
        loader = Mock(side_effect=[{"n": 1}, {"n": 2}, {"n": 3}])
        self.assertEqual(get_status(loader, max_age_seconds=0)[0], {"n": 1})
        self.assertEqual(get_status(loader, max_age_seconds=0)[0], {"n": 2})
        self.assertEqual(get_status(loader, force=True)[0], {"n": 3})
        self.assertEqual(loader.call_count, 3)

    def test_invalidation_and_query_errors(self):
        loader = Mock(return_value={"n": 1})
        get_status(loader)
        invalidate()
        get_status(loader)
        self.assertEqual(loader.call_count, 2)
        invalidate()
        broken = Mock(side_effect=RuntimeError("query failed"))
        with self.assertRaisesRegex(RuntimeError, "query failed"):
            get_status(broken)


if __name__ == "__main__":
    unittest.main()
