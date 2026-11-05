from __future__ import annotations

import sys
import unittest
import copy
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.virtual_output_pool.module import (
    IDLE_SECONDS, LEASE_STALE_SECONDS, PROFILES, OutputPoolError, OutputPoolModule, build_plan,
    mode_identity_key, rebind_renumbered_endpoints,
)
from modules.task_display_lease.authority import MAX_TTL_SECONDS as TASK_LEASE_TTL_SECONDS
from core.module_host import ModuleHost
from app import (ManagerApp, has_verified_physical_primary,
                 output_pool_confirmation_message, output_pool_status_line,
                 output_pool_topology_status_line, vdd_change_control_state)
from types import SimpleNamespace
from unittest.mock import Mock, patch


class FakeBackend:
    def __init__(self):
        self.count = 1
        self.windows = 0
        self.writes = []
        self.fail_write = False
        self.fail_read = False
        self.modes = {mode_identity_key("DISPLAY1", "MONITOR1", "PATH1"): (800, 600, 60)}
        self.fail_mode = False
        self.fail_restore = False

    def read_count(self):
        if self.fail_read:
            raise OSError("display path query failed")
        return self.count

    def write_count(self, count):
        self.writes.append(count)
        if self.fail_write:
            return False
        self.count = count
        self.modes = {mode_identity_key(f"DISPLAY{i}", f"MONITOR{i}", f"PATH{i}"):
                      self.modes.get(mode_identity_key(f"DISPLAY{i}", f"MONITOR{i}", f"PATH{i}"),
                                     (800, 600, 60))
                      for i in range(1, count + 1)}
        return "DELIVERED_NO_REPLY"

    def read_modes(self):
        return dict(self.modes)

    def write_mode(self, mode):
        if self.fail_mode:
            return False
        self.modes = {key: tuple(mode) for key in self.modes}
        return True

    def restore_modes(self, modes):
        if self.fail_restore:
            return False
        self.modes.update({key: tuple(mode) for key, mode in modes.items() if key in self.modes})
        return True

    def window_count(self):
        return self.windows


class FakeStore:
    def __init__(self, events=None):
        self.data = None
        self.fail_save = False
        self.events = events if events is not None else []

    def load(self):
        return copy.deepcopy(self.data)

    def save(self, value):
        if self.fail_save:
            raise OSError("disk full")
        self.events.append(("save", value["session"]["phase"]))
        self.data = copy.deepcopy(value)

    def clear(self):
        self.events.append(("clear", None))
        self.data = None


class VirtualOutputPoolTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.backend = FakeBackend()
        self.pool = OutputPoolModule(clock=lambda: self.now, require_empty_baseline=False)
        self.pool.configure_backend(count_reader=self.backend.read_count,
                                    count_writer=self.backend.write_count,
                                    mode_reader=self.backend.read_modes,
                                    mode_writer=self.backend.write_mode,
                                    mode_restorer=self.backend.restore_modes,
                                    output_window_count=self.backend.window_count)
        self.modes = set(PROFILES.values())

    def test_default_policy_refuses_any_preexisting_output_without_writes(self):
        pool = OutputPoolModule(clock=lambda: self.now)
        pool.configure_backend(count_reader=self.backend.read_count,
                               count_writer=self.backend.write_count,
                               mode_reader=self.backend.read_modes,
                               mode_writer=self.backend.write_mode,
                               mode_restorer=self.backend.restore_modes,
                               output_window_count=self.backend.window_count)
        original_modes = dict(self.backend.modes)
        with self.assertRaisesRegex(OutputPoolError, "zero pre-existing virtual outputs"):
            pool.begin(3, "high", self.modes, confirmed=True)
        self.assertEqual(self.backend.writes, [])
        self.assertEqual(self.backend.count, 1)
        self.assertEqual(self.backend.modes, original_modes)
        self.assertEqual(pool.status()["state"], "off")

    def test_default_policy_starts_from_zero_and_expires_to_zero(self):
        self.backend.count = 0
        self.backend.modes = {}
        pool = OutputPoolModule(clock=lambda: self.now)
        pool.configure_backend(count_reader=self.backend.read_count,
                               count_writer=self.backend.write_count,
                               mode_reader=self.backend.read_modes,
                               mode_writer=self.backend.write_mode,
                               mode_restorer=self.backend.restore_modes,
                               output_window_count=self.backend.window_count)
        result = pool.begin(2, "high", self.modes, confirmed=True)
        self.assertEqual(result["plan"]["previous_count"], 0)
        self.assertEqual(self.backend.count, 2)
        self.assertEqual(pool.reconcile()["state"], "idle_countdown")
        self.now += IDLE_SECONDS + 1
        expired = pool.reconcile()
        self.assertEqual(expired["state"], "off")
        self.assertEqual(expired["restored_count"], 0)
        self.assertEqual(self.backend.count, 0)

    def test_begin_waits_for_delayed_endpoint_enumeration_and_stable_identity(self):
        self.backend.count = 0
        self.backend.modes = {}
        pool = OutputPoolModule(clock=lambda: self.now)
        reads = 0

        def delayed_reader():
            nonlocal reads
            reads += 1
            if reads == 2:
                return {mode_identity_key("DISPLAY1", "MONITOR1", "PATH1"):
                        (800, 600, 60)}
            return self.backend.read_modes()

        pool.configure_backend(count_reader=self.backend.read_count,
                               count_writer=self.backend.write_count,
                               mode_reader=delayed_reader,
                               mode_writer=self.backend.write_mode,
                               mode_restorer=self.backend.restore_modes,
                               output_window_count=self.backend.window_count)
        with patch("modules.virtual_output_pool.module.time.sleep", return_value=None):
            result = pool.begin(2, "high", self.modes, confirmed=True)
        self.assertEqual(result["status"], "active")
        self.assertEqual(reads, 5)
        self.assertEqual(self.backend.writes, [2])
        self.assertEqual(self.backend.count, 2)

    def test_begin_persists_safe_reason_when_endpoint_identity_never_stabilizes(self):
        pool = OutputPoolModule(clock=lambda: self.now, require_empty_baseline=False)
        calls = 0

        def replaced_reader():
            nonlocal calls
            calls += 1
            if calls == 1:
                return self.backend.read_modes()
            return {mode_identity_key("DISPLAY1", "REPLACED_MONITOR", "REPLACED_PATH"):
                    (800, 600, 60),
                    mode_identity_key("DISPLAY2", "MONITOR2", "PATH2"):
                    (800, 600, 60),
                    mode_identity_key("DISPLAY3", "MONITOR3", "PATH3"):
                    (800, 600, 60)}

        store = FakeStore()
        pool.configure_persistence(store)
        pool.configure_backend(count_reader=self.backend.read_count,
                               count_writer=self.backend.write_count,
                               mode_reader=replaced_reader,
                               mode_writer=self.backend.write_mode,
                               mode_restorer=self.backend.restore_modes,
                               output_window_count=self.backend.window_count)
        with patch("modules.virtual_output_pool.module.time.sleep", return_value=None):
            with self.assertRaisesRegex(OutputPoolError, "did not stabilize"):
                pool.begin(3, "high", self.modes, confirmed=True)
        self.assertEqual(pool.status()["state"], "cleanup_pending")
        self.assertEqual(pool.status()["error"],
                         "endpoint_identity:baseline_endpoint_identity_mismatch")
        self.assertEqual(self.backend.writes, [3])
        self.assertEqual(self.backend.count, 3)

    def begin(self, count=3, profile="high"):
        return self.pool.begin(count, profile, self.modes, confirmed=True)

    def test_profiles_require_exact_supported_resolution_and_refresh(self):
        plan = build_plan(3, 1, "high", {(2560, 1440, 60), (1920, 1080, 60)})
        self.assertEqual((plan.width, plan.height, plan.refresh_rate), (2560, 1440, 60))
        with self.assertRaisesRegex(OutputPoolError, "exact ultra mode"):
            build_plan(3, 1, "ultra", {(3840, 2160, 24)})

    def test_count_and_profile_validation_are_fail_closed(self):
        for count in (0, 9, True, 2.0):
            with self.subTest(count=count), self.assertRaises(OutputPoolError):
                build_plan(count, 1, "high", self.modes)
        with self.assertRaisesRegex(OutputPoolError, "may not remove"):
            build_plan(1, 2, "high", self.modes)
        with self.assertRaisesRegex(OutputPoolError, "explicit in-app"):
            self.pool.begin(3, "high", self.modes, confirmed=False)
        with self.assertRaisesRegex(OutputPoolError, "at least two"):
            self.pool.begin(1, "high", self.modes, confirmed=True)

    def test_begin_applies_target_count_and_reports_baseline_for_expiry(self):
        result = self.begin()
        self.assertEqual(self.backend.count, 3)
        self.assertEqual(result["plan"]["previous_count"], 1)
        self.assertEqual(result["plan"]["outputs_to_remove_on_expiry"], 2)
        self.assertEqual(self.pool.status()["state"], "active")
        self.assertEqual(set(self.backend.modes.values()), {(2560, 1440, 60)})

    def test_begin_rebinds_baseline_when_windows_renumbers_display_source(self):
        old_key = mode_identity_key("DISPLAY32", "MONITOR1", "UID256")
        new_baseline = mode_identity_key("DISPLAY33", "MONITOR1", "UID256")
        self.backend.modes = {old_key: (1920, 1080, 60)}

        def renumbered_count_writer(count):
            self.backend.writes.append(count)
            self.backend.count = count
            self.backend.modes = {
                new_baseline: (1920, 1080, 60),
                mode_identity_key("DISPLAY34", "MONITOR6", "UID257"): (1920, 1080, 60),
                mode_identity_key("DISPLAY35", "MONITOR7", "UID258"): (1920, 1080, 60),
            }
            return True

        self.pool.configure_backend(count_reader=self.backend.read_count,
                                    count_writer=renumbered_count_writer,
                                    mode_reader=self.backend.read_modes,
                                    mode_writer=self.backend.write_mode,
                                    mode_restorer=self.backend.restore_modes,
                                    output_window_count=self.backend.window_count)
        self.begin(count=3)

        session = self.pool.snapshot()["session"]
        self.assertEqual(session["previous_modes"], {new_baseline: [1920, 1080, 60]})
        self.assertEqual(set(session["expected_endpoint_keys"]), set(self.backend.modes))
        lease = self.pool.task_leaseable_endpoint_keys(self.backend.read_modes())
        self.assertTrue(lease["eligible"])
        self.assertEqual(lease["endpoint_keys"], [
            mode_identity_key("DISPLAY34", "MONITOR6", "UID257"),
            mode_identity_key("DISPLAY35", "MONITOR7", "UID258"),
        ])

    def test_rebind_requires_same_unique_monitor_device_and_full_path(self):
        baseline = {mode_identity_key("DISPLAY32", "MONITOR1", "UID256"): [1920, 1080, 60]}
        self.assertEqual(rebind_renumbered_endpoints(
            baseline, {mode_identity_key("DISPLAY33", "MONITOR1", "UID256"): (1920, 1080, 60)}),
            {mode_identity_key("DISPLAY33", "MONITOR1", "UID256"): [1920, 1080, 60]})
        for replacement in (
            mode_identity_key("DISPLAY33", "MONITOR_REPLACED", "UID256"),
            mode_identity_key("DISPLAY33", "MONITOR1", "UID_REPLACED"),
        ):
            with self.subTest(replacement=replacement), self.assertRaisesRegex(OutputPoolError, "uniquely rebind"):
                rebind_renumbered_endpoints(baseline, {replacement: (1920, 1080, 60)})
        duplicate = {
            mode_identity_key("DISPLAY33", "MONITOR1", "UID256"): (1920, 1080, 60),
            mode_identity_key("DISPLAY99", "MONITOR1", "UID256"): (1920, 1080, 60),
        }
        with self.assertRaisesRegex(OutputPoolError, "uniquely rebind"):
            rebind_renumbered_endpoints(baseline, duplicate)

    def test_pending_identity_session_recovers_only_exact_profile_endpoints(self):
        old_baseline = mode_identity_key("DISPLAY32", "MONITOR1", "UID256")
        rebound_baseline = mode_identity_key("DISPLAY33", "MONITOR1", "UID256")
        added = [mode_identity_key("DISPLAY34", "MONITOR6", "UID257"),
                 mode_identity_key("DISPLAY35", "MONITOR7", "UID258")]
        self.backend.count = 3
        self.backend.modes = {rebound_baseline: (2560, 1440, 60),
                              added[0]: (2560, 1440, 60), added[1]: (2560, 1440, 60)}
        store = FakeStore()
        store.data = {"schema_version": 1, "saved_at_epoch": 1000.0, "session": {
            "previous_count": 1, "output_count": 3, "profile": "high",
            "previous_modes": {old_baseline: [1920, 1080, 60]},
            "phase": "startup_cleanup_pending", "error": "endpoint_identity:OutputPoolError",
            "expected_endpoint_keys": None, "started_age_seconds": 0.0,
            "last_activity_age_seconds": 1800.0, "lease_ages_seconds": {},
        }}
        self.pool.configure_persistence(store)

        result = self.pool.reconcile(now=self.now)

        session = self.pool.snapshot()["session"]
        self.assertEqual(session["phase"], "active")
        self.assertIsNone(session["error"])
        self.assertEqual(session["previous_modes"], {rebound_baseline: [1920, 1080, 60]})
        self.assertEqual(set(session["expected_endpoint_keys"]), set(self.backend.modes))
        self.assertEqual(result["state"], "idle_countdown")
        lease = self.pool.task_leaseable_endpoint_keys(self.backend.read_modes())
        self.assertTrue(lease["eligible"])
        self.assertEqual(lease["endpoint_keys"], added)
        self.assertEqual(self.backend.writes, [])

    def test_active_identity_session_recovers_only_exact_unique_renumbered_endpoints(self):
        old_baseline = mode_identity_key("DISPLAY32", "MONITOR1", "UID256")
        new_baseline = mode_identity_key("DISPLAY10", "MONITOR1", "UID256")
        old_added = [mode_identity_key("DISPLAY37", "MONITOR6", "UID257"),
                     mode_identity_key("DISPLAY38", "MONITOR7", "UID258")]
        new_added = [mode_identity_key("DISPLAY11", "MONITOR6", "UID257"),
                     mode_identity_key("DISPLAY12", "MONITOR7", "UID258")]
        self.backend.count = 3
        self.backend.modes = {new_baseline: (2560, 1440, 60),
                              new_added[0]: (2560, 1440, 60),
                              new_added[1]: (2560, 1440, 60)}
        store = FakeStore()
        store.data = {"schema_version": 1, "saved_at_epoch": 1000.0, "session": {
            "previous_count": 1, "output_count": 3, "profile": "high",
            "previous_modes": {old_baseline: [1920, 1080, 60]},
            "phase": "active", "error": None,
            "expected_endpoint_keys": [old_baseline, *old_added],
            "started_age_seconds": 0.0, "last_activity_age_seconds": 0.0,
            "lease_ages_seconds": {},
        }}
        self.pool.configure_persistence(store)

        result = self.pool.reconcile(now=self.now)

        session = self.pool.snapshot()["session"]
        self.assertEqual(session["phase"], "active")
        self.assertEqual(session["previous_modes"], {new_baseline: [1920, 1080, 60]})
        self.assertEqual(set(session["expected_endpoint_keys"]), set(self.backend.modes))
        self.assertEqual(result["state"], "idle_countdown")
        self.assertEqual(self.backend.writes, [])

    def test_active_identity_session_rejects_ambiguous_or_changed_endpoints_without_writes(self):
        old_baseline = mode_identity_key("DISPLAY32", "MONITOR1", "UID256")
        old_added = [mode_identity_key("DISPLAY37", "MONITOR6", "UID257"),
                     mode_identity_key("DISPLAY38", "MONITOR7", "UID258")]
        variants = (
            {mode_identity_key("DISPLAY10", "MONITOR_OTHER", "UID256"): (2560, 1440, 60),
             mode_identity_key("DISPLAY11", "MONITOR6", "UID257"): (2560, 1440, 60),
             mode_identity_key("DISPLAY12", "MONITOR7", "UID258"): (2560, 1440, 60)},
            {mode_identity_key("DISPLAY10", "MONITOR1", "UID256"): (2560, 1440, 60),
             mode_identity_key("DISPLAY11", "MONITOR6", "UID257"): (1920, 1080, 60),
             mode_identity_key("DISPLAY12", "MONITOR7", "UID258"): (2560, 1440, 60)},
            {mode_identity_key("DISPLAY10", "MONITOR1", "UID256"): (2560, 1440, 60),
             mode_identity_key("DISPLAY11", "MONITOR6", "UID257"): (2560, 1440, 60)},
        )
        for current_modes in variants:
            with self.subTest(current_modes=current_modes):
                store = FakeStore()
                store.data = {"schema_version": 1, "saved_at_epoch": 1000.0, "session": {
                    "previous_count": 1, "output_count": 3, "profile": "high",
                    "previous_modes": {old_baseline: [1920, 1080, 60]},
                    "phase": "active", "error": None,
                    "expected_endpoint_keys": [old_baseline, *old_added],
                    "started_age_seconds": 0.0, "last_activity_age_seconds": 0.0,
                    "lease_ages_seconds": {},
                }}
                self.backend.count = len(current_modes)
                self.backend.modes = dict(current_modes)
                pool = OutputPoolModule(clock=lambda: self.now, require_empty_baseline=False)
                pool.configure_backend(count_reader=self.backend.read_count,
                                       count_writer=self.backend.write_count,
                                       mode_reader=self.backend.read_modes,
                                       mode_writer=self.backend.write_mode,
                                       mode_restorer=self.backend.restore_modes,
                                       output_window_count=self.backend.window_count)
                pool.configure_persistence(store)
                result = pool.reconcile(now=self.now)
                self.assertEqual(result["state"], "active_unverified")
                self.assertEqual(pool.snapshot()["session"]["expected_endpoint_keys"],
                                 [old_baseline, *old_added])
                self.assertEqual(self.backend.writes, [])

    def test_pending_identity_session_rejects_wrong_mode_or_replaced_baseline(self):
        old_baseline = mode_identity_key("DISPLAY32", "MONITOR1", "UID256")
        replacement = mode_identity_key("DISPLAY33", "MONITOR_OTHER", "UID256")
        new1 = mode_identity_key("DISPLAY34", "MONITOR6", "UID257")
        new2 = mode_identity_key("DISPLAY35", "MONITOR7", "UID258")
        for baseline_key, baseline_mode, added_mode in (
            (replacement, (2560, 1440, 60), (2560, 1440, 60)),
            (mode_identity_key("DISPLAY33", "MONITOR1", "UID256"),
             (1920, 1080, 60), (1920, 1080, 60)),
        ):
            with self.subTest(baseline_key=baseline_key, added_mode=added_mode):
                self.backend.count = 3
                self.backend.modes = {baseline_key: baseline_mode,
                                      new1: added_mode, new2: (2560, 1440, 60)}
                store = FakeStore()
                store.data = {"schema_version": 1, "saved_at_epoch": 1000.0, "session": {
                    "previous_count": 1, "output_count": 3, "profile": "high",
                    "previous_modes": {old_baseline: [1920, 1080, 60]},
                    "phase": "startup_cleanup_pending", "error": "endpoint_identity:OutputPoolError",
                    "expected_endpoint_keys": None, "started_age_seconds": 0.0,
                    "last_activity_age_seconds": 1800.0, "lease_ages_seconds": {},
                }}
                pool = OutputPoolModule(clock=lambda: self.now)
                pool.configure_backend(count_reader=self.backend.read_count,
                                        count_writer=self.backend.write_count,
                                        mode_reader=self.backend.read_modes,
                                        mode_writer=self.backend.write_mode,
                                        mode_restorer=self.backend.restore_modes,
                                        output_window_count=self.backend.window_count)
                pool.configure_persistence(store)
                result = pool.reconcile(now=self.now)
                self.assertEqual(pool.snapshot()["session"]["expected_endpoint_keys"], None)
                self.assertEqual(result["state"], "active_unverified")
                self.assertFalse(pool.task_leaseable_endpoint_keys(self.backend.read_modes())["eligible"])
                self.assertEqual(self.backend.writes, [])

    def test_begin_requires_at_least_one_new_output_and_does_not_reconfigure_baseline_only(self):
        self.backend.count = 2
        self.backend.modes[mode_identity_key("DISPLAY2", "MONITOR2", "PATH2")] = (800, 600, 60)
        baseline = dict(self.backend.modes)
        with self.assertRaisesRegex(OutputPoolError, "at least one new output"):
            self.begin(count=2)
        self.assertEqual(self.backend.writes, [])
        self.assertEqual(self.backend.modes, baseline)
        self.assertEqual(self.pool.status()["state"], "off")

    def test_begin_refuses_endpoint_identity_change_between_count_and_mode_commit(self):
        replacement = mode_identity_key("DISPLAY3", "MONITOR_REPLACED", "PATH_REPLACED")

        def replace_during_mode_apply(mode):
            old_key = mode_identity_key("DISPLAY3", "MONITOR3", "PATH3")
            self.backend.modes[replacement] = self.backend.modes.pop(old_key)
            self.backend.modes = {key: tuple(mode) for key in self.backend.modes}
            return True

        self.pool.configure_backend(count_reader=self.backend.read_count,
                                    count_writer=self.backend.write_count,
                                    mode_reader=self.backend.read_modes,
                                    mode_writer=replace_during_mode_apply,
                                    mode_restorer=self.backend.restore_modes,
                                    output_window_count=self.backend.window_count)
        with self.assertRaisesRegex(OutputPoolError, "endpoint identities changed"):
            self.begin(count=3)
        self.assertEqual(self.backend.count, 3)
        self.assertEqual(self.backend.writes, [3])
        self.assertIn(replacement, self.backend.modes)
        self.assertEqual(self.pool.status()["state"], "cleanup_pending")

    def test_task_lease_exposes_only_pool_created_outputs(self):
        self.begin(count=3)
        result = self.pool.task_leaseable_endpoint_keys(self.backend.read_modes())
        self.assertTrue(result["eligible"])
        self.assertEqual(result["endpoint_keys"], [
            mode_identity_key("DISPLAY2", "MONITOR2", "PATH2"),
            mode_identity_key("DISPLAY3", "MONITOR3", "PATH3"),
        ])

    def test_task_lease_policy_matches_confirmed_count_profile_and_new_endpoints(self):
        self.begin(count=3, profile="high")
        modes = self.backend.read_modes()
        policy = self.pool.task_lease_policy(modes, 2, "high")
        self.assertTrue(policy["eligible"])
        self.assertEqual(policy["output_count"], 2)
        self.assertEqual(policy["endpoint_keys"], [
            mode_identity_key("DISPLAY2", "MONITOR2", "PATH2"),
            mode_identity_key("DISPLAY3", "MONITOR3", "PATH3"),
        ])
        self.assertEqual(self.pool.task_lease_policy(modes, 3, "high")["error_code"],
                         "OUTPUT_COUNT_MISMATCH")
        self.assertEqual(self.pool.task_lease_policy(modes, 2, "ultra")["error_code"],
                         "OUTPUT_PROFILE_MISMATCH")

    def test_task_lease_fails_closed_without_pool_or_with_external_topology_delta(self):
        self.assertEqual(self.pool.task_leaseable_endpoint_keys(self.backend.read_modes()),
                         {"eligible": False, "error_code": "OUTPUT_POOL_NOT_VERIFIED"})
        self.begin(count=3)
        altered = self.backend.read_modes()
        altered.pop(mode_identity_key("DISPLAY1", "MONITOR1", "PATH1"))
        self.assertEqual(self.pool.task_leaseable_endpoint_keys(altered)["error_code"],
                         "POOL_BASELINE_ENDPOINT_MISSING")
        altered = self.backend.read_modes()
        altered[mode_identity_key("DISPLAY4", "MONITOR4", "PATH4")] = (2560, 1440, 60)
        self.assertEqual(self.pool.task_leaseable_endpoint_keys(altered)["error_code"],
                         "NO_POOL_CREATED_ENDPOINT")

    def test_task_lease_rejects_same_count_replaced_endpoint_identity(self):
        self.begin(count=3)
        changed = self.backend.read_modes()
        old_key = mode_identity_key("DISPLAY3", "MONITOR3", "PATH3")
        replacement = mode_identity_key("DISPLAY3", "MONITOR_REPLACED", "PATH_REPLACED")
        changed[replacement] = changed.pop(old_key)
        result = self.pool.task_leaseable_endpoint_keys(changed)
        self.assertEqual(result["error_code"], "OUTPUT_ENDPOINT_IDENTITY_UNVERIFIED")

    def test_failed_apply_does_not_claim_active_session(self):
        self.backend.fail_write = True
        with self.assertRaisesRegex(OutputPoolError, "did not confirm"):
            self.begin()
        self.assertEqual(self.pool.status()["state"], "off")


    def test_failed_quality_apply_rolls_back_added_outputs_and_mode(self):
        self.backend.fail_mode = True
        with self.assertRaisesRegex(OutputPoolError, "quality mode was not applied"):
            self.begin()
        self.assertEqual(self.backend.count, 1)
        self.assertEqual(self.backend.modes, {mode_identity_key("DISPLAY1", "MONITOR1", "PATH1"):
                                              (800, 600, 60)})
        self.assertEqual(self.pool.status()["state"], "off")

    def test_uncertain_startup_rollback_is_retried_automatically(self):
        self.backend.fail_mode = True
        self.backend.fail_restore = True
        with self.assertRaisesRegex(OutputPoolError, "quality mode was not applied"):
            self.begin()
        self.assertEqual(self.pool.status()["state"], "cleanup_pending")
        self.assertEqual(self.backend.count, 3)
        self.backend.fail_restore = False
        result = self.pool.reconcile()
        self.assertEqual(result["state"], "off")
        self.assertEqual(self.backend.count, 1)
        self.assertEqual(self.backend.modes,
                         {mode_identity_key("DISPLAY1", "MONITOR1", "PATH1"): (800, 600, 60)})

    def test_startup_rollback_refuses_to_remove_unexpected_endpoint(self):
        unexpected = mode_identity_key("DISPLAY_EXTERNAL", "MONITOR_EXTERNAL", "PATH_EXTERNAL")

        def mutate_then_fail(_mode):
            self.backend.modes[unexpected] = (2560, 1440, 60)
            return False

        self.pool.configure_backend(count_reader=self.backend.read_count,
                                    count_writer=self.backend.write_count,
                                    mode_reader=self.backend.read_modes,
                                    mode_writer=mutate_then_fail,
                                    mode_restorer=self.backend.restore_modes,
                                    output_window_count=self.backend.window_count)

        with self.assertRaisesRegex(OutputPoolError, "endpoint identities changed"):
            self.begin(count=3)

        self.assertEqual(self.pool.status()["state"], "cleanup_pending")
        self.assertEqual(self.backend.count, 3)
        self.assertIn(unexpected, self.backend.modes)
        self.assertEqual(self.backend.writes, [3])

    def test_authorized_lease_heartbeat_prevents_expiry(self):
        self.begin()
        with self.assertRaisesRegex(OutputPoolError, "explicitly authorized"):
            self.pool.heartbeat("only-chat-task")
        self.pool.heartbeat("only-chat-task", confirmed=True)
        self.now += LEASE_STALE_SECONDS - 1
        state = self.pool.reconcile()
        self.assertEqual(state["state"], "active")
        self.assertEqual(state["leases"], 1)
        self.assertEqual(self.backend.count, 3)

    def test_authorized_lease_survives_beyond_output_idle_grace(self):
        self.begin()
        self.pool.heartbeat("job-1", confirmed=True)
        self.now += IDLE_SECONDS + 1
        self.pool.heartbeat("job-1", confirmed=True)
        state = self.pool.reconcile()
        self.assertEqual(state["state"], "active")
        self.assertEqual(state["leases"], 1)
        self.assertEqual(self.backend.count, 3)

    def test_release_starts_thirty_minute_idle_countdown_then_restores(self):
        self.begin()
        self.pool.heartbeat("job-1", confirmed=True)
        self.now += 120
        self.pool.release("job-1")
        self.now += IDLE_SECONDS - 1
        self.assertEqual(self.pool.reconcile()["state"], "idle_countdown")
        self.assertEqual(self.backend.count, 3)
        self.now += 1
        result = self.pool.reconcile()
        self.assertEqual(result["state"], "off")
        self.assertEqual(self.backend.count, 1)
        self.assertEqual(self.backend.modes, {mode_identity_key("DISPLAY1", "MONITOR1", "PATH1"):
                                              (800, 600, 60)})
        self.assertEqual(self.backend.writes, [3, 1])

    def test_stale_lease_expires_then_idle_outputs_restore_automatically(self):
        self.begin()
        self.pool.heartbeat("job-1", confirmed=True)
        self.now += IDLE_SECONDS + 1
        state = self.pool.reconcile()
        self.assertEqual(state["state"], "off")
        self.assertEqual(state["leases"], 0)
        self.assertEqual(self.backend.count, 1)

    def test_open_window_keeps_output_alive_and_restarts_thirty_minute_idle_clock(self):
        self.begin()
        self.backend.windows = 1
        self.now += IDLE_SECONDS * 2
        state = self.pool.reconcile()
        self.assertEqual(state["state"], "active")
        self.assertEqual(self.backend.count, 3)
        self.assertEqual(state["idle_remaining_seconds"], IDLE_SECONDS)
        self.backend.windows = 0
        self.now += IDLE_SECONDS - 1
        self.assertEqual(self.pool.reconcile()["state"], "idle_countdown")
        self.now += 1
        expired = self.pool.reconcile()
        self.assertEqual(expired["state"], "off")
        self.assertEqual(expired["restored_count"], 1)
        self.assertEqual(self.backend.count, 1)

    def test_confirmed_early_stop_preserves_active_window_or_lease(self):
        self.begin()
        writes_before = list(self.backend.writes)
        self.backend.windows = 1
        window_result = self.pool.reconcile(force=True, confirmed=True)
        self.assertEqual(window_result["state"], "active")
        self.assertEqual(window_result["reason"], "active_leases_or_windows")
        self.assertEqual(self.backend.count, 3)

        self.backend.windows = 0
        self.pool.heartbeat("active-task", confirmed=True)
        lease_result = self.pool.reconcile(force=True, confirmed=True)
        self.assertEqual(lease_result["state"], "active")
        self.assertEqual(lease_result["reason"], "active_leases_or_windows")
        self.assertEqual(lease_result["leases"], 1)
        self.assertEqual(self.backend.count, 3)
        self.assertEqual(self.backend.writes, writes_before)

    def test_unverifiable_count_or_window_state_never_closes_outputs(self):
        self.begin()
        self.now += IDLE_SECONDS + 1
        self.backend.fail_read = True
        self.assertEqual(self.pool.reconcile()["state"], "active_unverified")
        self.assertEqual(self.backend.count, 3)

    def test_idle_expiry_attempt_failure_retains_recoverable_session(self):
        self.begin()
        self.now += IDLE_SECONDS + 1
        self.backend.fail_write = True
        writes_before = list(self.backend.writes)
        result = self.pool.reconcile()
        self.assertEqual(result["state"], "cleanup_unverified")
        self.assertEqual(self.pool.status()["state"], "cleanup_pending")
        self.assertEqual(self.backend.writes, writes_before + [1])
        self.backend.fail_write = False
        self.assertEqual(self.pool.reconcile()["state"], "off")
        self.assertEqual(self.backend.count, 1)

    def test_active_session_blocks_dispose_and_restore_revalidates_ledger(self):
        self.begin()
        with self.assertRaisesRegex(RuntimeError, "prevents disposal"):
            self.pool.dispose()
        saved = self.pool.snapshot()
        restored = OutputPoolModule(clock=lambda: self.now)
        restored.restore(saved)
        self.assertEqual(restored.status()["output_count"], 3)
        saved["session"]["unknown"] = True
        with self.assertRaisesRegex(OutputPoolError, "unknown or missing"):
            restored.restore(saved)

    def test_module_host_preserves_live_pool_on_reload_or_app_shutdown(self):
        host = ModuleHost(ROOT)
        info = host.load("virtual_output_pool")
        instance = host._loaded["virtual_output_pool"].instance
        instance.restore({"session": {
            "previous_count": 1, "output_count": 3, "profile": "high",
            "previous_modes": {mode_identity_key("DISPLAY1", "MONITOR1", "PATH1"):
                               [800, 600, 60]},
            "started_at": 10.0, "last_activity": 10.0, "leases": {},
            "phase": "active", "error": None,
        }})
        with self.assertRaisesRegex(RuntimeError, "prevents reload"):
            host.reload("virtual_output_pool")
        self.assertEqual(host.info("virtual_output_pool"), info)
        with self.assertRaisesRegex(RuntimeError, "prevents manager disposal"):
            host.dispose_all()
        instance.restore({"session": None})
        host.dispose_all()

    def test_standard_refresh_contract_is_read_only_and_reports_capability(self):
        pool = OutputPoolModule(clock=lambda: self.now)
        before = pool.snapshot()
        result = pool.refresh()
        self.assertEqual(result["state"], "off")
        self.assertEqual(result["module_id"], "virtual_output_pool")
        self.assertEqual(result["module_version"], "0.1.16")
        self.assertEqual(result["max_outputs"], 8)
        self.assertEqual(result["profiles"]["high"], [2560, 1440, 60])
        self.assertFalse(result["backend_configured"])
        self.assertEqual(pool.snapshot(), before)

    def test_startup_recovery_journal_is_committed_before_display_mutation(self):
        events = []
        store = FakeStore(events)
        self.pool.configure_persistence(store)
        original_writer = self.backend.write_count
        def write_count(count):
            events.append(("count", count))
            return original_writer(count)
        self.pool.configure_backend(count_reader=self.backend.read_count,
                                    count_writer=write_count,
                                    mode_reader=self.backend.read_modes,
                                    mode_writer=self.backend.write_mode,
                                    mode_restorer=self.backend.restore_modes,
                                    output_window_count=self.backend.window_count)
        self.begin()
        self.assertEqual(events[0], ("save", "startup_cleanup_pending"))
        self.assertEqual(events[1], ("count", 3))
        self.assertEqual(store.data["session"]["phase"], "active")

    def test_recovery_journal_write_failure_prevents_any_display_change(self):
        store = FakeStore()
        store.fail_save = True
        self.pool.configure_persistence(store)
        with self.assertRaisesRegex(OutputPoolError, "settings were not changed"):
            self.begin()
        self.assertEqual(self.backend.count, 1)
        self.assertEqual(self.backend.writes, [])
        self.assertEqual(self.pool.status()["state"], "recovery_required")

    def test_restarted_manager_automatically_restores_expired_persisted_pool(self):
        store = FakeStore()
        self.pool.configure_persistence(store)
        self.begin()
        raw = store.data
        resumed = OutputPoolModule(clock=lambda: 5000.0)
        resumed.configure_persistence(store)
        resumed.configure_backend(count_reader=self.backend.read_count,
                                  count_writer=self.backend.write_count,
                                  mode_reader=self.backend.read_modes,
                                  mode_writer=self.backend.write_mode,
                                  mode_restorer=self.backend.restore_modes,
                                  output_window_count=self.backend.window_count)
        # Simulate a manager restart after the durable idle deadline elapsed.
        raw["saved_at_epoch"] -= IDLE_SECONDS + 1
        resumed.restore_persisted(raw, wall_clock=raw["saved_at_epoch"] + IDLE_SECONDS + 1)
        result = resumed.reconcile(now=5000.0)
        self.assertEqual(result["state"], "off")
        self.assertEqual(self.backend.count, 1)
        self.assertIsNone(store.data)

    def test_restore_verification_rejects_unexpected_extra_endpoint(self):
        self.begin()
        self.now += IDLE_SECONDS + 1
        normal_writer = self.backend.write_count

        def writer_with_external_endpoint(count):
            result = normal_writer(count)
            if count == 1:
                self.backend.modes[mode_identity_key("DISPLAY_EXTRA", "MONITOR_EXTRA", "PATH_EXTRA")] = (
                    2560, 1440, 60
                )
            return result

        self.pool.configure_backend(count_reader=self.backend.read_count,
                                    count_writer=writer_with_external_endpoint,
                                    mode_reader=self.backend.read_modes,
                                    mode_writer=self.backend.write_mode,
                                    mode_restorer=self.backend.restore_modes,
                                    output_window_count=self.backend.window_count)
        result = self.pool.reconcile()
        self.assertEqual(result["state"], "cleanup_unverified")
        self.assertEqual(result["reason"], "baseline_mode_verification_failed")
        self.assertEqual(self.backend.count, 1)
        self.assertEqual(len(self.backend.modes), 2)
        self.assertEqual(self.pool.status()["state"], "cleanup_pending")

    def test_expiry_does_not_reduce_count_if_endpoint_identity_changes_during_mode_restore(self):
        self.begin()
        self.now += IDLE_SECONDS + 1
        original_restorer = self.backend.restore_modes
        original_writer = self.backend.write_count
        def restorer_with_endpoint_swap(modes):
            result = original_restorer(modes)
            key = mode_identity_key("DISPLAY3", "MONITOR3", "PATH3")
            replacement = mode_identity_key("DISPLAY3", "MONITOR3_NEW", "PATH3_NEW")
            self.backend.modes.pop(key, None)
            self.backend.modes[replacement] = (800, 600, 60)
            return result
        self.pool.configure_backend(count_reader=self.backend.read_count,
                                    count_writer=original_writer,
                                    mode_reader=self.backend.read_modes,
                                    mode_writer=self.backend.write_mode,
                                    mode_restorer=restorer_with_endpoint_swap,
                                    output_window_count=self.backend.window_count)

        result = self.pool.reconcile()

        self.assertEqual(result["state"], "cleanup_unverified")
        self.assertEqual(result["reason"], "endpoint_identity_changed_during_mode_restore")
        self.assertEqual(self.backend.count, 3)
        self.assertEqual(len(self.backend.modes), 3)
        self.assertEqual(self.backend.writes, [3])
        self.assertEqual(self.pool.status()["state"], "cleanup_pending")

    def test_expiry_serializes_against_a_concurrent_new_task_lease(self):
        self.begin()
        self.now += IDLE_SECONDS + 1
        entered_restore = threading.Event()
        allow_restore_to_finish = threading.Event()
        heartbeat_attempted = threading.Event()
        heartbeat_finished = threading.Event()
        expiry_results = []
        heartbeat_results = []
        original_restorer = self.backend.restore_modes

        def blocking_restorer(modes):
            entered_restore.set()
            if not allow_restore_to_finish.wait(2):
                raise TimeoutError("test restore gate was not released")
            return original_restorer(modes)

        self.pool.configure_backend(count_reader=self.backend.read_count,
                                    count_writer=self.backend.write_count,
                                    mode_reader=self.backend.read_modes,
                                    mode_writer=self.backend.write_mode,
                                    mode_restorer=blocking_restorer,
                                    output_window_count=self.backend.window_count)

        def expire():
            expiry_results.append(self.pool.reconcile())

        def heartbeat():
            heartbeat_attempted.set()
            try:
                heartbeat_results.append(self.pool.heartbeat("racing-task", confirmed=True))
            except Exception as exc:
                heartbeat_results.append(exc)
            finally:
                heartbeat_finished.set()

        expiry_thread = threading.Thread(target=expire, daemon=True)
        expiry_thread.start()
        self.assertTrue(entered_restore.wait(1), "expiry did not enter the mode restore phase")
        lease_thread = threading.Thread(target=heartbeat, daemon=True)
        lease_thread.start()
        self.assertTrue(heartbeat_attempted.wait(1), "lease thread did not start")
        try:
            self.assertFalse(heartbeat_finished.wait(0.05),
                             "new lease entered while expiry was changing topology")
        finally:
            allow_restore_to_finish.set()
        expiry_thread.join(2)
        lease_thread.join(2)

        self.assertFalse(expiry_thread.is_alive())
        self.assertFalse(lease_thread.is_alive())
        self.assertEqual(expiry_results[0]["state"], "off")
        self.assertEqual(len(heartbeat_results), 1)
        self.assertIsInstance(heartbeat_results[0], OutputPoolError)
        self.assertIn("No output-pool session", str(heartbeat_results[0]))
        self.assertEqual(self.backend.count, 1)
        self.assertEqual(self.backend.writes, [3, 1])

    def test_idle_grace_is_thirty_minutes_and_task_lease_staleness_matches_contract(self):
        self.assertEqual(IDLE_SECONDS, 30 * 60)
        self.assertEqual(LEASE_STALE_SECONDS, TASK_LEASE_TTL_SECONDS)
        self.assertEqual(LEASE_STALE_SECONDS, 120)

    def test_cleanup_pending_pool_refuses_new_lease_heartbeats(self):
        self.begin()
        self.pool._session["phase"] = "startup_cleanup_pending"
        with self.assertRaisesRegex(OutputPoolError, "not accepting leases during cleanup"):
            self.pool.heartbeat("late-task", confirmed=True)
        self.assertEqual(self.pool.status()["leases"], 0)

    def test_startup_rollback_does_not_reduce_count_if_endpoint_identity_changes_during_mode_restore(self):
        original_reader = self.backend.read_modes
        read_count = 0
        def reader_fails_after_mode_apply():
            nonlocal read_count
            read_count += 1
            if read_count == 4:
                raise OSError("injected post-apply observation failure")
            return original_reader()
        original_restorer = self.backend.restore_modes
        def restorer_with_endpoint_swap(modes):
            result = original_restorer(modes)
            key = mode_identity_key("DISPLAY3", "MONITOR3", "PATH3")
            replacement = mode_identity_key("DISPLAY3", "MONITOR3_NEW", "PATH3_NEW")
            self.backend.modes.pop(key, None)
            self.backend.modes[replacement] = (800, 600, 60)
            return result
        self.pool.configure_backend(count_reader=self.backend.read_count,
                                    count_writer=self.backend.write_count,
                                    mode_reader=reader_fails_after_mode_apply,
                                    mode_writer=self.backend.write_mode,
                                    mode_restorer=restorer_with_endpoint_swap,
                                    output_window_count=self.backend.window_count)

        with self.assertRaisesRegex(OutputPoolError, "Could not verify"):
            self.begin()

        self.assertEqual(self.backend.count, 3)
        self.assertEqual(len(self.backend.modes), 3)
        self.assertEqual(self.backend.writes, [3])
        self.assertEqual(self.pool.status()["state"], "cleanup_pending")

    def test_legacy_journal_without_endpoint_ids_is_readable_but_never_authorizes_writes(self):
        store = FakeStore()
        self.pool.configure_persistence(store)
        self.begin()
        store.data["session"].pop("expected_endpoint_keys")
        resumed = OutputPoolModule(clock=lambda: self.now + IDLE_SECONDS + 1)
        resumed.configure_persistence(store)
        resumed.configure_backend(count_reader=self.backend.read_count,
                                  count_writer=self.backend.write_count,
                                  mode_reader=self.backend.read_modes,
                                  mode_writer=self.backend.write_mode,
                                  mode_restorer=self.backend.restore_modes,
                                  output_window_count=self.backend.window_count)
        writes_before = list(self.backend.writes)

        result = resumed.reconcile()

        self.assertEqual(result["state"], "active_unverified")
        self.assertEqual(result["reason"], "display_configuration_changed_since_pool_request")
        self.assertEqual(self.backend.count, 3)
        self.assertEqual(self.backend.writes, writes_before)

    def test_reconcile_refuses_to_overwrite_external_display_change(self):
        self.begin()
        self.now += IDLE_SECONDS + 1
        external_modes = {mode_identity_key("DISPLAY1", "MONITOR1", "PATH1"):
                          (1920, 1080, 60)}
        self.backend.count = 1
        self.backend.modes = dict(external_modes)
        writes_before = list(self.backend.writes)

        result = self.pool.reconcile()

        self.assertEqual(result["state"], "active_unverified")
        self.assertEqual(result["reason"], "display_configuration_changed_since_pool_request")
        self.assertEqual(self.backend.count, 1)
        self.assertEqual(self.backend.modes, external_modes)
        self.assertEqual(self.backend.writes, writes_before)
        self.assertEqual(self.pool.status()["state"], "active")

    def test_reconcile_refuses_same_count_external_mode_edit_without_writes(self):
        self.begin(count=2)
        self.now += IDLE_SECONDS + 1
        changed = dict(self.backend.modes)
        first = next(iter(changed))
        changed[first] = (1920, 1080, 60)
        self.backend.modes = changed
        writes_before = list(self.backend.writes)

        result = self.pool.reconcile()

        self.assertEqual(result["state"], "active_unverified")
        self.assertEqual(result["reason"], "display_configuration_changed_since_pool_request")
        self.assertEqual(self.backend.count, 2)
        self.assertEqual(self.backend.modes, changed)
        self.assertEqual(self.backend.writes, writes_before)

    def test_reconcile_refuses_same_count_replaced_endpoint_identity_without_writes(self):
        self.begin(count=3)
        self.now += IDLE_SECONDS + 1
        old_key = mode_identity_key("DISPLAY3", "MONITOR3", "PATH3")
        replacement_key = mode_identity_key("DISPLAY3", "MONITOR3_REPLACED", "PATH3_REPLACED")
        self.assertIn(old_key, self.backend.modes)
        self.backend.modes.pop(old_key)
        self.backend.modes[replacement_key] = (2560, 1440, 60)
        changed = dict(self.backend.modes)
        writes_before = list(self.backend.writes)

        result = self.pool.reconcile()

        self.assertEqual(result["state"], "active_unverified")
        self.assertEqual(result["reason"], "display_configuration_changed_since_pool_request")
        self.assertEqual(self.backend.count, 3)
        self.assertEqual(self.backend.modes, changed)
        self.assertEqual(self.backend.writes, writes_before)

    def test_reconcile_refuses_external_output_count_expansion_without_writes(self):
        self.begin(count=2)
        self.now += IDLE_SECONDS + 1
        self.backend.count = 3
        self.backend.modes[mode_identity_key("DISPLAY3", "MONITOR3", "PATH3")] = (1920, 1080, 60)
        changed = dict(self.backend.modes)
        writes_before = list(self.backend.writes)

        result = self.pool.reconcile()

        self.assertEqual(result["state"], "active_unverified")
        self.assertEqual(result["reason"], "display_configuration_changed_since_pool_request")
        self.assertEqual(self.backend.count, 3)
        self.assertEqual(self.backend.modes, changed)
        self.assertEqual(self.backend.writes, writes_before)

    def test_stale_journal_clears_without_writing_already_restored_baseline(self):
        store = FakeStore()
        self.pool.configure_persistence(store)
        self.begin()
        # Simulate external/manual restoration completing just before recovery.
        self.backend.count = 1
        self.backend.modes = {mode_identity_key("DISPLAY1", "MONITOR1", "PATH1"):
                              (800, 600, 60)}
        writes_before = list(self.backend.writes)

        result = self.pool.reconcile(now=self.now + IDLE_SECONDS + 1)

        self.assertEqual(result["state"], "off")
        self.assertTrue(result["expired"])
        self.assertEqual(self.backend.writes, writes_before)
        self.assertIsNone(store.data)

    def test_persistence_failure_during_heartbeat_disables_automatic_expiry(self):
        store = FakeStore()
        self.pool.configure_persistence(store)
        self.begin()
        store.fail_save = True
        previous = self.pool.snapshot()
        with self.assertRaisesRegex(OutputPoolError, "persist the output lease heartbeat"):
            self.pool.heartbeat("task", confirmed=True)
        self.assertEqual(self.pool.snapshot(), previous)
        self.now += IDLE_SECONDS + 1
        result = self.pool.reconcile()
        self.assertEqual(result["state"], "cleanup_unverified")
        self.assertEqual(self.backend.count, 3)

    def test_persistence_failure_during_release_retains_the_lease(self):
        store = FakeStore()
        self.pool.configure_persistence(store)
        self.begin()
        self.pool.heartbeat("task", confirmed=True)
        before = self.pool.snapshot()
        store.fail_save = True
        with self.assertRaisesRegex(OutputPoolError, "persist output lease release"):
            self.pool.release("task")
        self.assertEqual(self.pool.snapshot(), before)

    def test_count_change_is_not_reverted_when_endpoint_identity_snapshot_is_unavailable(self):
        self.backend.fail_read = False
        original_read = self.backend.read_count
        calls = 0

        def read_then_fail():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("display query raced the driver")
            return original_read()

        self.pool.configure_backend(count_reader=read_then_fail,
                                    count_writer=self.backend.write_count,
                                    mode_reader=self.backend.read_modes,
                                    mode_writer=self.backend.write_mode,
                                    mode_restorer=self.backend.restore_modes,
                                    output_window_count=self.backend.window_count)
        with self.assertRaisesRegex(OutputPoolError, "Could not verify"):
            self.begin()
        self.assertEqual(self.backend.writes, [3])
        self.assertEqual(self.backend.count, 3)
        self.assertEqual(self.pool.status()["state"], "cleanup_pending")


class OutputPoolUiCopyTests(unittest.TestCase):
    def test_apply_confirmation_describes_idle_auto_restore_and_fail_closed_state(self):
        text = output_pool_confirmation_message(2, "high")
        self.assertIn("2560×1440 @ 60 Hz", text)
        self.assertIn("5초", text)
        self.assertIn("자동 복구", text)
        self.assertIn("5초", text)
        self.assertIn("최대 15초 주기", text)
        self.assertIn("열린 창", text)
        self.assertIn("정확히 확인할 수 없으면 복구를 시도하지 않고", text)
        self.assertIn("수동으로 일찍 반환할 때는 별도 확인", text)
        with_existing = output_pool_confirmation_message(3, "high", previous_count=1)
        self.assertIn("현재 1개에서 AI 작업용 출력 2개를 추가", with_existing)

    def test_status_distinguishes_countdown_cleanup_and_uncertain_state(self):
        active = output_pool_status_line({"state": "idle_countdown", "output_count": 2,
                                          "profile": "high", "idle_remaining_seconds": 1740})
        self.assertIn("29분 뒤 정리 대상", active)
        self.assertIn("최대 15초", active)
        self.assertIn("열린 창/AI 작업 중에는 대기", active)
        leased = output_pool_status_line({"state": "active", "leases": 1,
                                          "output_count": 2, "profile": "high",
                                          "idle_remaining_seconds": 5})
        self.assertIn("AI 작업 lease 유지 중", leased)
        cleanup = output_pool_status_line({"state": "cleanup_pending"})
        self.assertIn("자동 복구 진행 중", cleanup)
        uncertain = output_pool_status_line({"state": "cleanup_unverified"})
        self.assertIn("변경하지 않고 보존", uncertain)
        self.assertNotIn("자동 복구 진행 중", uncertain)

    @staticmethod
    def _topology(*, physical=True, virtual=True, cloned=False, identity=True,
                  topology_verified=True):
        displays = []
        if physical:
            displays.append({"display_name": "\\\\.\\DISPLAY1", "monitor_path": "DELL-PATH",
                             "attached": True, "verified": True,
                             "topology_verified": topology_verified,
                             "monitor_identity_verified": identity, "virtual": False,
                             "primary": True, "cloned": cloned,
                             "mirroring_driver": False, "x": 0, "y": 0,
                             "width": 2560, "height": 1440})
        if virtual:
            displays.append({"display_name": "\\\\.\\DISPLAY36", "monitor_path": "MTT-PATH",
                             "attached": True, "verified": True,
                             "topology_verified": topology_verified,
                             "monitor_identity_verified": identity, "virtual": True,
                             "primary": not physical, "cloned": cloned,
                             "mirroring_driver": False, "monitor_parent": "ROOT\\DISPLAY\\0000",
                             "x": 2560 if physical else 0, "y": 0,
                             "width": 1920, "height": 1080})
        return {"topology_verified": topology_verified,
                "mtt_device_instance_id": "ROOT\\DISPLAY\\0000", "displays": displays}

    def test_verified_physical_primary_requires_real_uncloned_identity(self):
        self.assertTrue(has_verified_physical_primary(self._topology()))
        self.assertFalse(has_verified_physical_primary(self._topology(physical=False)))
        self.assertFalse(has_verified_physical_primary(self._topology(cloned=True)))
        self.assertFalse(has_verified_physical_primary(self._topology(identity=False)))
        self.assertFalse(has_verified_physical_primary(
            self._topology(topology_verified=False)))

    def test_pool_status_reports_active_unowned_mtt_output_and_locks_no_physical(self):
        topology = self._topology(physical=False)
        line = output_pool_topology_status_line({"state": "off", "leases": 0}, topology)
        self.assertIn("관리 세션 밖의 MTT 가상 화면 1개", line)
        self.assertIn("소유권이 확인되지 않아 자동 정리하지 않습니다", line)
        self.assertIn("검증된 물리 주 화면이 없어 VDD 변경 잠김", line)

    def test_vdd_change_controls_fail_closed_without_physical_primary(self):
        controls = vdd_change_control_state(
            self._topology(physical=False), {"state": "off"},
            driver_installed=True, pipe_connected=True)
        self.assertEqual(controls, {"enable": False, "disable": False, "start": False,
                                    "single": False, "stop": False})

    def test_vdd_change_controls_do_not_disable_driver_with_active_unowned_output(self):
        controls = vdd_change_control_state(
            self._topology(), {"state": "off"}, driver_installed=True,
            pipe_connected=True)
        self.assertTrue(controls["start"])
        self.assertFalse(controls["disable"])
        self.assertFalse(controls["stop"])

    def test_manager_manual_count_change_refuses_before_driver_command_without_physical_primary(self):
        app = object.__new__(ManagerApp)
        device = SimpleNamespace(command=Mock())
        app.host = SimpleNamespace(_loaded={"mtt_vdd": SimpleNamespace(instance=device)})
        app._pool_output_count = lambda: 1
        with patch("modules.app_window_router.module.current_guard_topology",
                   return_value=self._topology(physical=False)):
            with self.assertRaisesRegex(RuntimeError, "PHYSICAL_PRIMARY_UNVERIFIED"):
                app._pool_set_output_count(0)
        device.command.assert_not_called()

    def test_manual_cli_pool_begin_refuses_before_dispatch_without_physical_primary(self):
        app = object.__new__(ManagerApp)
        app.host = SimpleNamespace(invoke=Mock())
        with patch("modules.app_window_router.module.current_guard_topology",
                   return_value=self._topology(physical=False)):
            with self.assertRaisesRegex(RuntimeError, "PHYSICAL_PRIMARY_UNVERIFIED"):
                app._pool_begin_with_physical_guard({"confirmed": True})
        app.host.invoke.assert_not_called()

    def test_manual_stop_count_refuses_before_driver_command_without_physical_primary(self):
        app = object.__new__(ManagerApp)
        device = SimpleNamespace(command=Mock())
        app.host = SimpleNamespace(_loaded={"mtt_vdd": SimpleNamespace(instance=device)})
        with patch("modules.app_window_router.module.current_guard_topology",
                   return_value=self._topology(physical=False)):
            with self.assertRaisesRegex(RuntimeError, "PHYSICAL_PRIMARY_UNVERIFIED"):
                app._guarded_manual_count_change(0)
        device.command.assert_not_called()

    def test_pool_preview_uses_configured_evidence_only_when_no_mtt_outputs_exist(self):
        app = object.__new__(ManagerApp)
        configured = {"modes": [[2560, 1440, 60]],
                      "source": "configured_vdd_settings", "sha256": "a" * 64}
        reader = Mock(return_value=configured)
        app.host = SimpleNamespace(_loaded={
            "manager_cli": SimpleNamespace(instance=SimpleNamespace(
                configured_output_mode_evidence=reader))})
        app._verified_virtual_outputs = lambda: ("ROOT\\DISPLAY\\0000", [])

        result = app._pool_supported_mode_evidence()

        self.assertEqual(result, {"modes": [(2560, 1440, 60)],
                                  "source": "configured_vdd_settings", "sha256": "a" * 64})
        reader.assert_called_once_with()

    def test_pool_preview_retains_live_common_mode_evidence_with_active_outputs(self):
        app = object.__new__(ManagerApp)
        reader = Mock()
        app.host = SimpleNamespace(_loaded={
            "manager_cli": SimpleNamespace(instance=SimpleNamespace(
                configured_output_mode_evidence=reader))})
        app._verified_virtual_outputs = lambda: ("ROOT\\DISPLAY\\0000", [
            {"available_modes": [[1920, 1080, 60], [2560, 1440, 60]]},
            {"available_modes": [[1920, 1080, 60]]},
        ])

        result = app._pool_supported_mode_evidence()

        self.assertEqual(result, {"modes": [(1920, 1080, 60)],
                                  "source": "active_mtt_outputs", "sha256": None})
        reader.assert_not_called()

    def test_unsupported_profile_cannot_form_confirmation_copy(self):
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            output_pool_confirmation_message(2, "unknown")


if __name__ == "__main__":
    unittest.main(verbosity=2)

