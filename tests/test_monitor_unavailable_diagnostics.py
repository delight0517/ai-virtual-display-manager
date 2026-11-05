import unittest

from modules.app_window_router.physical_guard import _diagnostic_topology


class MonitorUnavailableDiagnosticsTests(unittest.TestCase):
    def test_pending_identity_refresh_is_explained_and_stays_unavailable(self):
        result = _diagnostic_topology({
            "topology_verified": False,
            "monitor_identity_refresh_state": "refreshing",
            "monitor_identity_age_ms": 5400,
            "monitor_identity_cache_hit": True,
            "displays": [{"monitor_identity_verified": False, "virtual": None}],
        })
        self.assertEqual(result["result"], "unavailable")
        self.assertEqual(result["output_count"], 0)
        self.assertEqual(result["reason"], "monitor_identity_refresh_pending")
        self.assertEqual(result["monitor_identity_age_ms"], 5400)
        self.assertTrue(result["monitor_identity_cache_hit"])

    def test_failed_refresh_does_not_log_exception_details(self):
        result = _diagnostic_topology({
            "topology_verified": False,
            "monitor_identity_refresh_state": "error",
            "monitor_identity_error_type": "TimeoutError",
            "error": "private path and driver text",
            "displays": [],
        })
        self.assertEqual(result["reason"], "monitor_identity_refresh_failed")
        self.assertNotIn("error", result)
        self.assertNotIn("TimeoutError", str(result))

    def test_verified_physical_topology_stays_successful(self):
        result = _diagnostic_topology({
            "topology_verified": True,
            "monitor_identity_refresh_state": "fresh",
            "displays": [{"attached": True, "verified": True,
                          "topology_verified": True,
                          "monitor_identity_verified": True,
                          "cloned": False, "virtual": False}],
        })
        self.assertEqual(result["result"], "succeeded")
        self.assertIsNone(result["reason"])
        self.assertEqual(result["topology"], "physical_only")

    def test_provider_exception_is_distinguished_without_message(self):
        result = _diagnostic_topology({
            "topology_verified": False,
            "provider_failure": True,
            "provider_error_type": "TimeoutError",
            "error": "C:\\private\\path and raw exception text",
            "displays": [],
        })
        self.assertEqual(result["reason"], "topology_provider_exception")
        self.assertEqual(result["provider_error_type"], "TimeoutError")
        self.assertNotIn("private", str(result))
        self.assertNotIn("error", result)

    def test_unclassified_failure_has_specific_safe_fallback(self):
        result = _diagnostic_topology({"topology_verified": False, "displays": []})
        self.assertEqual(result["reason"], "monitor_identity_unverified")


if __name__ == "__main__":
    unittest.main()
