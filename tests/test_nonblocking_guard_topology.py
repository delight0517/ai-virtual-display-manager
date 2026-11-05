from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import Mock, patch

from core.mtt_status_cache import (
    get_status,
    get_status_nonblocking,
    invalidate,
    wait_for_refresh,
)
from modules.app_window_router.module import AppWindowRouter, current_guard_topology
from modules.mtt_vdd.module import MttVddModule


class NonblockingStatusCacheTests(unittest.TestCase):
    def setUp(self):
        invalidate()
        self.addCleanup(invalidate)
        self.addCleanup(lambda: self.assertTrue(wait_for_refresh(2.0)))

    def test_expired_cache_returns_immediately_and_coalesces_refresh(self):
        entered = threading.Event()
        release = threading.Event()
        loader = Mock(side_effect=lambda: (entered.set(), release.wait(2.0), {"n": 1})[-1])

        started = time.perf_counter()
        first = get_status_nonblocking(loader, max_age_seconds=0)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.1)
        self.assertEqual(first[0], None)
        self.assertEqual(first[3], "refreshing")
        self.assertTrue(entered.wait(1.0))

        second = get_status_nonblocking(loader, max_age_seconds=0)
        self.assertEqual(second[3], "refreshing")
        loader.assert_called_once()
        release.set()
        self.assertTrue(wait_for_refresh(2.0))
        self.assertEqual(get_status(loader, max_age_seconds=5)[0], {"n": 1})
        loader.assert_called_once()

    def test_early_refresh_preserves_fresh_snapshot_until_ttl(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def loader():
            calls.append(len(calls) + 1)
            if len(calls) == 2:
                entered.set()
                release.wait(2.0)
            return {"n": len(calls)}

        self.assertEqual(get_status(loader)[0], {"n": 1})
        first = get_status_nonblocking(loader, max_age_seconds=30,
                                       refresh_early_seconds=30)
        self.assertEqual((first[0], first[3]), ({"n": 1}, "fresh"))
        self.assertTrue(entered.wait(1.0))
        during = get_status_nonblocking(loader, max_age_seconds=30,
                                        refresh_early_seconds=30)
        self.assertEqual((during[0], during[3]), ({"n": 1}, "fresh"))
        release.set()
        self.assertTrue(wait_for_refresh(2.0))
        self.assertEqual(get_status(loader)[0], {"n": 2})
        self.assertEqual(calls, [1, 2])

    def test_failed_early_refresh_never_extends_expired_snapshot(self):
        calls = []

        def loader():
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                return {"n": 1}
            raise RuntimeError("refresh failed")

        self.assertEqual(get_status(loader)[0], {"n": 1})
        result = get_status_nonblocking(loader, max_age_seconds=30,
                                        refresh_early_seconds=30)
        self.assertEqual((result[0], result[3]), ({"n": 1}, "fresh"))
        self.assertTrue(wait_for_refresh(2.0))
        expired = get_status_nonblocking(loader, max_age_seconds=0,
                                         refresh_early_seconds=0)
        self.assertEqual(expired[3], "error")
        self.assertEqual(expired[4], "RuntimeError")

    def test_physical_guard_gets_longer_pnp_read_budget_than_status_operations(self):
        module = MttVddModule()
        with patch("modules.mtt_vdd.module.get_status_nonblocking",
                   return_value=({}, time.monotonic(), True, "fresh", None)) as read:
            module.refresh(nonblocking=True)
        self.assertEqual(read.call_args.kwargs["max_age_seconds"], 20.0)
        self.assertEqual(read.call_args.kwargs["refresh_early_seconds"], 18.0)

    def test_refresh_failure_is_reported_and_backed_off(self):
        loader = Mock(side_effect=RuntimeError("PnP unavailable"))
        result = get_status_nonblocking(loader, max_age_seconds=0)
        self.assertEqual(result[3], "refreshing")
        self.assertTrue(wait_for_refresh(2.0))

        failed = get_status_nonblocking(loader, max_age_seconds=0)
        self.assertEqual(failed[3], "error")
        self.assertEqual(failed[4], "RuntimeError")
        loader.assert_called_once()

    def test_invalidation_discards_inflight_observation(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def loader():
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                entered.set()
                release.wait(2.0)
                return {"state": "before-mutation"}
            return {"state": "after-mutation"}

        self.assertEqual(get_status_nonblocking(loader, max_age_seconds=0)[3], "refreshing")
        self.assertTrue(entered.wait(1.0))
        invalidate()
        release.set()
        self.assertTrue(wait_for_refresh(2.0))
        self.assertEqual(get_status(loader)[0], {"state": "after-mutation"})
        self.assertEqual(calls, [1, 2])


class _FakeMttModule:
    def __init__(self, status):
        self.status = status

    def refresh(self, *, nonblocking=False):
        self.nonblocking = nonblocking
        return dict(self.status)


class _FakeHost:
    def __init__(self, status, topology):
        self._loaded = {"mtt_vdd": type("Loaded", (), {"instance": _FakeMttModule(status)})()}
        self.topology = topology
        self.disposed = False

    def load(self, *args, **kwargs):
        return None

    def refresh(self, module_id):
        return {"topology_verified": True,
                "displays": [dict(row) for row in self.topology["displays"]],
                "error": ""}

    def dispose_all(self):
        self.disposed = True


class GuardTopologyTests(unittest.TestCase):
    def _run_topology(self, status, *, nonblocking=True):
        topology = {"displays": [{
            "display_name": "DISPLAY1",
            "monitor_path": r"\\?\DISPLAY#ABC#1234#{GUID}",
            "topology_verified": True,
            "virtual": True,
            "monitor_parent": "old-parent",
            "monitor_identity_verified": True,
        }]}
        host = _FakeHost(status, topology)
        with patch("core.module_host.ModuleHost", return_value=host):
            result = current_guard_topology(nonblocking=nonblocking)
        return result, host

    def test_stale_pnp_identity_cannot_authorize_guard_or_move(self):
        state, _ = self._run_topology({
            "status_age_ms": 5001,
            "status_cache_hit": True,
            "status_refresh_state": "refreshing",
            "monitors": [{"InstanceId": r"DISPLAY\ABC\1234", "Virtual": True}],
            "device": {"instance_id": "ROOT\\DISPLAY\\0000"},
        })
        self.assertFalse(state["topology_verified"])
        self.assertEqual(state["monitor_identity_refresh_state"], "refreshing")
        self.assertFalse(state["displays"][0]["monitor_identity_verified"])
        self.assertIsNone(state["displays"][0]["virtual"])
        self.assertEqual(state["displays"][0]["monitor_parent"], "")

    def test_fresh_pnp_identity_remains_usable(self):
        state, _ = self._run_topology({
            "status_age_ms": 0,
            "status_cache_hit": False,
            "status_refresh_state": "fresh",
            "monitors": [{"InstanceId": r"DISPLAY\ABC\1234",
                          "Virtual": False, "Parent": "ROOT\\DISPLAY\\0001"}],
            "device": {"instance_id": "ROOT\\DISPLAY\\0000"},
        })
        self.assertTrue(state["topology_verified"])
        self.assertTrue(state["displays"][0]["monitor_identity_verified"])
        self.assertFalse(state["displays"][0]["virtual"])

    def test_one_shot_topology_keeps_synchronous_fresh_read(self):
        _, host = self._run_topology({
            "status_age_ms": 0,
            "status_cache_hit": False,
            "status_refresh_state": "fresh",
            "monitors": [],
            "device": {},
        }, nonblocking=False)
        self.assertFalse(host._loaded["mtt_vdd"].instance.nonblocking)

    def test_guard_uses_nonblocking_default_but_one_shot_provider_stays_sync(self):
        router = AppWindowRouter(window_api=object())
        self.assertIsNot(router.guard_display_provider, router.display_provider)

        custom_provider = Mock(return_value={"topology_verified": False, "displays": []})
        custom = AppWindowRouter(window_api=object(), display_provider=custom_provider)
        self.assertIs(custom.guard_display_provider, custom_provider)
        self.assertIs(custom.display_provider, custom_provider)


if __name__ == "__main__":
    unittest.main()
