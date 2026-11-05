"""Fail-closed policy and lease lifecycle for demand-based MTT virtual outputs.

This module deliberately owns no Windows display calls. The manager must inject
an explicitly confirmed, verified backend before it can mutate display state.
"""
from __future__ import annotations

import json
import math
import threading
import time
from functools import wraps
from dataclasses import dataclass
from typing import Callable

from modules.task_display_lease.authority import MAX_TTL_SECONDS as TASK_LEASE_TTL_SECONDS

API_VERSION = 1
MODULE_ID = "virtual_output_pool"
VERSION = "0.1.15"
# Keep output cleanup close to the last active task/window. The manager polls
# pool state every 15 seconds, so this is a short handoff grace, not a timer.
IDLE_SECONDS = 5
# The authenticated consumer lease has a separate 120-second TTL. Do not let
# the short output cleanup grace expire a still-live task lease.
LEASE_STALE_SECONDS = TASK_LEASE_TTL_SECONDS
MAX_OUTPUTS = 8
ENDPOINT_DISCOVERY_ATTEMPTS = 51
ENDPOINT_DISCOVERY_INTERVAL_SECONDS = 0.1

# These exact tuples must also be reported as supported by the output mode
# enumerator; the policy never silently chooses a lower resolution or refresh.
PROFILES = {
    "balanced": (1920, 1080, 60),
    "high": (2560, 1440, 60),
    "ultra": (3840, 2160, 30),
}


class OutputPoolError(ValueError):
    def __init__(self, message, *, reason_code=None):
        super().__init__(message)
        self.reason_code = reason_code


def _serialized(method):
    """Serialize pool leases against expiry/rollback topology transitions."""
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


@dataclass(frozen=True)
class OutputPlan:
    output_count: int
    previous_count: int
    profile: str
    width: int
    height: int
    refresh_rate: int

    def as_dict(self):
        return {
            "output_count": self.output_count,
            "previous_count": self.previous_count,
            "profile": self.profile,
            "width": self.width,
            "height": self.height,
            "refresh_rate": self.refresh_rate,
            "outputs_to_add": max(0, self.output_count - self.previous_count),
            "outputs_to_remove_on_expiry": max(0, self.output_count - self.previous_count),
        }


def build_plan(output_count, previous_count, profile, supported_modes):
    """Validate count/profile against exact active-output mode evidence."""
    if isinstance(output_count, bool) or not isinstance(output_count, int) or not 1 <= output_count <= MAX_OUTPUTS:
        raise OutputPoolError(f"output_count must be an integer from 1 to {MAX_OUTPUTS}")
    if isinstance(previous_count, bool) or not isinstance(previous_count, int) or not 0 <= previous_count <= MAX_OUTPUTS:
        raise OutputPoolError("previous_count is outside the driver's supported range")
    if not isinstance(profile, str) or profile not in PROFILES:
        raise OutputPoolError("Unknown resolution profile")
    if output_count < previous_count:
        raise OutputPoolError("A managed request may not remove pre-existing virtual outputs")
    mode = PROFILES[profile]
    if not isinstance(supported_modes, (set, frozenset, list, tuple)):
        raise OutputPoolError("Supported mode evidence is missing")
    normalized = set()
    try:
        for item in supported_modes:
            if len(item) != 3:
                continue
            w, h, hz = item
            if any(isinstance(v, bool) for v in (w, h, hz)):
                continue
            normalized.add((int(w), int(h), int(hz)))
    except (TypeError, ValueError):
        raise OutputPoolError("Supported mode evidence is malformed") from None
    if mode not in normalized:
        raise OutputPoolError(f"The exact {profile} mode {mode[0]}x{mode[1]}@{mode[2]} is not supported")
    return OutputPlan(output_count, previous_count, profile, *mode)


def mode_identity_key(display_name, monitor_device_id, monitor_path):
    values = tuple(str(value).strip() for value in (display_name, monitor_device_id, monitor_path))
    if not all(values) or any("\n" in value or "\r" in value for value in values):
        raise OutputPoolError("A stable display endpoint identity is required")
    return "\n".join(values)


def parse_mode_identity_key(value):
    if not isinstance(value, str):
        raise OutputPoolError("Invalid display-mode identity")
    parts = value.split("\n")
    if len(parts) != 3 or not all(part.strip() for part in parts) or any("\r" in part for part in parts):
        raise OutputPoolError("Invalid display-mode identity")
    return tuple(parts)


def rebind_renumbered_endpoints(previous_modes, current_modes):
    """Rebind only Windows source-name changes with identical unique PnP identity."""
    if not isinstance(previous_modes, dict) or not isinstance(current_modes, dict):
        raise OutputPoolError("Output endpoint identity evidence is missing")
    current_by_stable_id = {}
    for key in current_modes:
        _, device_id, monitor_path = parse_mode_identity_key(key)
        current_by_stable_id.setdefault((device_id, monitor_path), []).append(key)
    rebound = {}
    used = set()
    for old_key, mode in previous_modes.items():
        _, device_id, monitor_path = parse_mode_identity_key(old_key)
        matches = current_by_stable_id.get((device_id, monitor_path), ())
        if len(matches) != 1 or matches[0] in used:
            raise OutputPoolError("Could not uniquely rebind a pre-existing output endpoint")
        used.add(matches[0])
        rebound[matches[0]] = list(mode)
    return rebound


class OutputPoolModule:
    """Lease state machine. External adapters are provided by the manager."""

    def __init__(self, clock: Callable[[], float] | None = None,
                 count_reader: Callable[[], int] | None = None,
                 count_writer: Callable[[int], object] | None = None,
                 mode_reader: Callable[[], dict] | None = None,
                 mode_writer: Callable[[tuple[int, int, int]], object] | None = None,
                 mode_restorer: Callable[[dict], object] | None = None,
                 output_window_count: Callable[[], int] | None = None,
                 require_empty_baseline: bool = True):
        self._clock = clock or __import__("time").monotonic
        self._lock = threading.RLock()
        self._count_reader = count_reader
        self._count_writer = count_writer
        self._mode_reader = mode_reader
        self._mode_writer = mode_writer
        self._mode_restorer = mode_restorer
        self._output_window_count = output_window_count
        self._require_empty_baseline = require_empty_baseline is True
        self._session = None
        self._disposed = False
        self._persistence = None
        self._persistence_error = None

    @_serialized
    def init(self):
        self._disposed = False

    @_serialized
    def snapshot(self):
        return json.loads(json.dumps({"session": self._session}))

    @_serialized
    def restore(self, state):
        raw = state.get("session") if isinstance(state, dict) else None
        if raw is None:
            self._session = None
            return
        if not isinstance(raw, dict):
            raise OutputPoolError("Invalid saved output-pool session")
        required = {"previous_count", "output_count", "profile", "previous_modes",
                    "started_at", "last_activity", "leases", "phase", "error"}
        if not required.issubset(raw) or set(raw) - required - {"expected_endpoint_keys"}:
            raise OutputPoolError("Saved output-pool session has unknown or missing fields")
        build_plan(raw["output_count"], raw["previous_count"], raw["profile"], tuple(PROFILES.values()))
        leases = raw["leases"]
        if not isinstance(leases, dict) or len(leases) > 128:
            raise OutputPoolError("Invalid output-pool lease ledger")
        for key, stamp in leases.items():
            if not isinstance(key, str) or not key or len(key) > 128:
                raise OutputPoolError("Invalid output-pool lease ID")
            if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
                raise OutputPoolError("Invalid output-pool lease timestamp")
        modes = raw["previous_modes"]
        if not isinstance(modes, dict) or len(modes) != raw["previous_count"]:
            raise OutputPoolError("Invalid baseline display-mode snapshot")
        for name, mode in modes.items():
            parse_mode_identity_key(name)
            if not isinstance(mode, (list, tuple)) or len(mode) != 3:
                raise OutputPoolError("Invalid baseline display-mode entry")
            if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in mode):
                raise OutputPoolError("Invalid baseline display-mode values")
        endpoint_keys = raw.get("expected_endpoint_keys")
        if endpoint_keys is not None:
            if (not isinstance(endpoint_keys, list) or len(endpoint_keys) != raw["output_count"]
                    or any(not isinstance(key, str) for key in endpoint_keys)
                    or len(set(endpoint_keys)) != len(endpoint_keys)):
                raise OutputPoolError("Invalid expected output endpoint identities")
            for key in endpoint_keys:
                parse_mode_identity_key(key)
            if not set(modes).issubset(endpoint_keys):
                raise OutputPoolError("Expected output endpoints omit a pre-existing endpoint")
        for field in ("started_at", "last_activity"):
            if isinstance(raw[field], bool) or not isinstance(raw[field], (int, float)) or not math.isfinite(raw[field]):
                raise OutputPoolError("Invalid output-pool timestamp")
        if raw["phase"] not in {"active", "startup_cleanup_pending"}:
            raise OutputPoolError("Invalid output-pool lifecycle phase")
        if raw["error"] is not None and (not isinstance(raw["error"], str) or len(raw["error"]) > 200):
            raise OutputPoolError("Invalid output-pool error state")
        self._session = json.loads(json.dumps(raw))

    @_serialized
    def configure_backend(self, *, count_reader, count_writer, mode_reader, mode_writer,
                          mode_restorer, output_window_count):
        if not all(callable(x) for x in (count_reader, count_writer, mode_reader,
                                         mode_writer, mode_restorer, output_window_count)):
            raise OutputPoolError("All verified output-pool backend callbacks are required")
        self._count_reader = count_reader
        self._count_writer = count_writer
        self._mode_reader = mode_reader
        self._mode_writer = mode_writer
        self._mode_restorer = mode_restorer
        self._output_window_count = output_window_count

    @_serialized
    def configure_persistence(self, store):
        if not all(callable(getattr(store, name, None)) for name in ("load", "save", "clear")):
            raise OutputPoolError("A load/save/clear recovery store is required")
        self._persistence = store
        try:
            raw = store.load()
            if raw is not None:
                self.restore_persisted(raw)
        except Exception as exc:
            self._persistence_error = f"{type(exc).__name__}: recovery journal could not be restored"[:200]

    @_serialized
    def persisted_snapshot(self, *, wall_clock=None):
        if self._session is None:
            return None
        now = self._clock()
        wall = __import__("time").time() if wall_clock is None else wall_clock
        session = json.loads(json.dumps(self._session))
        session["started_age_seconds"] = max(0.0, now - session.pop("started_at"))
        session["last_activity_age_seconds"] = max(0.0, now - session.pop("last_activity"))
        session["lease_ages_seconds"] = {
            key: max(0.0, now - stamp) for key, stamp in session.pop("leases").items()
        }
        return {"schema_version": 1, "saved_at_epoch": wall, "session": session}

    @_serialized
    def restore_persisted(self, raw, *, wall_clock=None):
        if not isinstance(raw, dict) or set(raw) != {"schema_version", "saved_at_epoch", "session"}:
            raise OutputPoolError("Invalid persisted output-pool journal envelope")
        if raw["schema_version"] != 1 or isinstance(raw["saved_at_epoch"], bool) \
                or not isinstance(raw["saved_at_epoch"], (int, float)) \
                or not math.isfinite(raw["saved_at_epoch"]):
            raise OutputPoolError("Invalid persisted output-pool journal version or timestamp")
        session = raw["session"]
        if not isinstance(session, dict):
            raise OutputPoolError("Persisted output-pool session is missing")
        required = {"previous_count", "output_count", "profile", "previous_modes", "phase", "error",
                    "started_age_seconds", "last_activity_age_seconds", "lease_ages_seconds"}
        if not required.issubset(session) or set(session) - required - {"expected_endpoint_keys"}:
            raise OutputPoolError("Persisted output-pool session fields are invalid")
        wall = __import__("time").time() if wall_clock is None else wall_clock
        elapsed = max(0.0, wall - raw["saved_at_epoch"])
        now = self._clock()
        def age(value):
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value) or value < 0:
                raise OutputPoolError("Persisted idle age is invalid")
            return min(366 * 24 * 60 * 60, value + elapsed)
        leases = session["lease_ages_seconds"]
        if not isinstance(leases, dict) or len(leases) > 128:
            raise OutputPoolError("Persisted lease ages are invalid")
        restored = {key: value for key, value in session.items()
                    if key not in {"started_age_seconds", "last_activity_age_seconds", "lease_ages_seconds"}}
        # Older schema-v1 journals stay readable, but missing endpoint identity
        # evidence cannot authorize restoration writes.
        restored.setdefault("expected_endpoint_keys", None)
        restored["started_at"] = now - age(session["started_age_seconds"])
        restored["last_activity"] = now - age(session["last_activity_age_seconds"])
        restored["leases"] = {key: now - age(value) for key, value in leases.items()}
        self.restore({"session": restored})

    @_serialized
    def _save_journal(self):
        if self._persistence is None:
            return True
        try:
            if self._session is None:
                self._persistence.clear()
            else:
                self._persistence.save(self.persisted_snapshot())
            self._persistence_error = None
            return True
        except Exception as exc:
            self._persistence_error = f"{type(exc).__name__}: recovery journal write failed"[:200]
            return False

    def _await_stable_endpoint_modes(self, output_count, previous_modes):
        """Wait briefly for Windows display-path enumeration after a count change.

        The MTT count pipe can acknowledge a new count before QueryDisplayConfig
        exposes every endpoint. Require the exact set to be valid and identical
        across two consecutive observations; never infer ownership from count
        alone. The bounded wait runs on the pool worker, not the UI thread.
        """
        prior_keys = None
        last_code = "endpoint_snapshot_unavailable"
        for attempt in range(ENDPOINT_DISCOVERY_ATTEMPTS):
            try:
                current_modes = self._mode_reader()
                if not isinstance(current_modes, dict):
                    last_code = "endpoint_snapshot_invalid"
                else:
                    endpoint_keys = sorted(current_modes)
                    if len(endpoint_keys) != output_count:
                        last_code = "endpoint_count_mismatch"
                    else:
                        for key in endpoint_keys:
                            parse_mode_identity_key(key)
                        try:
                            rebound_baseline = rebind_renumbered_endpoints(previous_modes, current_modes)
                        except OutputPoolError:
                            last_code = "baseline_endpoint_identity_mismatch"
                        else:
                            if endpoint_keys == prior_keys:
                                return current_modes, rebound_baseline, endpoint_keys
                            prior_keys = endpoint_keys
                            last_code = "endpoint_identity_not_stable"
            except OutputPoolError:
                last_code = "endpoint_identity_invalid"
            except Exception:
                last_code = "endpoint_snapshot_unavailable"
            if attempt + 1 < ENDPOINT_DISCOVERY_ATTEMPTS:
                time.sleep(ENDPOINT_DISCOVERY_INTERVAL_SECONDS)
        raise OutputPoolError(
            "The active display endpoints did not stabilize within the bounded discovery window",
            reason_code=last_code,
        )

    @_serialized
    def begin(self, output_count, profile, supported_modes, *, confirmed=False):
        if self._disposed:
            raise OutputPoolError("Output pool is disposed")
        if self._persistence_error:
            raise OutputPoolError("Recovery journal requires attention before a new output session")
        if confirmed is not True:
            raise OutputPoolError("An explicit in-app output-pool confirmation is required")
        if self._session is not None:
            raise OutputPoolError("An output-pool session is already active")
        if isinstance(output_count, bool) or not isinstance(output_count, int) or output_count < 2:
            raise OutputPoolError("An AI output-pool session requires at least two virtual outputs")
        if not all((self._count_reader, self._count_writer, self._mode_reader,
                    self._mode_writer, self._mode_restorer, self._output_window_count)):
            raise OutputPoolError("No verified Windows output backend is configured")
        previous = self._count_reader()
        if self._require_empty_baseline and previous != 0:
            raise OutputPoolError(
                "A managed AI output session requires zero pre-existing virtual outputs; "
                "existing outputs remain untouched"
            )
        if output_count <= previous:
            raise OutputPoolError("A managed output pool must add at least one new output")
        plan = build_plan(output_count, previous, profile, supported_modes)
        previous_modes = self._mode_reader()
        if not isinstance(previous_modes, dict) or len(previous_modes) != previous:
            raise OutputPoolError("Could not snapshot every pre-existing virtual output mode")
        for name, mode in previous_modes.items():
            parse_mode_identity_key(name)
            if (not isinstance(mode, (list, tuple))
                    or len(mode) != 3 or any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in mode)):
                raise OutputPoolError("The pre-existing output mode snapshot is invalid")
        now = self._clock()
        self._session = {
            "previous_count": previous,
            "output_count": output_count,
            "profile": profile,
            "previous_modes": {key: list(value) for key, value in previous_modes.items()},
            "started_at": now,
            "last_activity": now - IDLE_SECONDS,
            "leases": {},
            "phase": "startup_cleanup_pending",
            "error": "startup_interrupted_before_commit",
            "expected_endpoint_keys": None,
        }
        if not self._save_journal():
            self._session = None
            raise OutputPoolError("Could not persist recovery state; display settings were not changed")
        count_may_have_changed = False
        if output_count > previous:
            count_may_have_changed = True
            try:
                result = self._count_writer(output_count)
            except Exception as exc:
                self._rollback_or_retain(previous, output_count, profile, previous_modes,
                                         f"count_start:{type(exc).__name__}")
                raise OutputPoolError(f"The output-count request failed: {type(exc).__name__}") from None
            if result is False:
                self._rollback_or_retain(previous, output_count, profile, previous_modes,
                                         "count_start_not_confirmed")
                raise OutputPoolError("The driver did not confirm the output-count request")
            try:
                applied_count = self._count_reader()
            except Exception as exc:
                self._rollback_or_retain(previous, output_count, profile, previous_modes,
                                         f"count_verify:{type(exc).__name__}")
                raise OutputPoolError(f"Could not verify the applied output count: {type(exc).__name__}") from None
            if applied_count != output_count:
                self._rollback_or_retain(previous, output_count, profile, previous_modes,
                                         "count_mismatch")
                raise OutputPoolError("The requested output count did not become active")
            try:
                counted_modes, rebound_baseline, endpoint_keys = self._await_stable_endpoint_modes(
                    output_count, previous_modes)
                previous_modes = rebound_baseline
                self._session["previous_modes"] = {
                    key: list(value) for key, value in previous_modes.items()
                }
                self._session["expected_endpoint_keys"] = endpoint_keys
                if not self._save_journal():
                    raise OutputPoolError("Could not persist output endpoint identities")
            except Exception as exc:
                self._rollback_or_retain(previous, output_count, profile, previous_modes,
                                         f"endpoint_identity:{getattr(exc, 'reason_code', None) or type(exc).__name__}")
                if isinstance(exc, OutputPoolError):
                    raise
                raise OutputPoolError(f"Could not verify output endpoint identities: {type(exc).__name__}") from None
        mode_may_have_changed = True
        try:
            desired = (plan.width, plan.height, plan.refresh_rate)
            mode_result = self._mode_writer(desired)
            current_modes = self._mode_reader()
            expected_endpoint_keys = self._session.get("expected_endpoint_keys")
            if (not isinstance(expected_endpoint_keys, list)
                    or not isinstance(current_modes, dict)
                    or set(current_modes) != set(expected_endpoint_keys)):
                raise OutputPoolError("Output endpoint identities changed while applying the requested profile")
            if (mode_result is False or not isinstance(current_modes, dict)
                    or len(current_modes) != output_count
                    or any(tuple(mode) != desired for mode in current_modes.values())):
                raise OutputPoolError("The requested quality mode was not applied to every virtual output")
        except Exception as exc:
            self._rollback_or_retain(previous, output_count, profile, previous_modes,
                                     f"mode_apply:{type(exc).__name__}",
                                     restore_mode=mode_may_have_changed,
                                     restore_count=count_may_have_changed)
            if isinstance(exc, OutputPoolError):
                raise
            raise OutputPoolError(f"Could not verify the requested quality mode: {type(exc).__name__}") from None
        now = self._clock()
        self._session = {
            "previous_count": previous,
            "output_count": output_count,
            "profile": profile,
            "previous_modes": {key: list(value) for key, value in previous_modes.items()},
            "started_at": now,
            "last_activity": now,
            "leases": {},
            "phase": "active",
            "error": None,
            "expected_endpoint_keys": list(self._session.get("expected_endpoint_keys")),
        }
        if not self._save_journal():
            self._rollback_or_retain(previous, output_count, profile, previous_modes,
                                     "active_session_persist_failed")
            raise OutputPoolError("Could not persist the active output session; verified rollback was attempted")
        return {"status": "active", "plan": plan.as_dict(), "idle_seconds": IDLE_SECONDS}

    @_serialized
    def _rollback_or_retain(self, previous_count, requested_count, profile, previous_modes,
                            error, *, restore_mode=True, restore_count=True):
        verified = False
        expected_endpoint_keys = (
            list(self._session.get("expected_endpoint_keys"))
            if isinstance(self._session, dict)
            and isinstance(self._session.get("expected_endpoint_keys"), list)
            else None
        )
        try:
            current = self._count_reader()
            if (isinstance(current, bool) or not isinstance(current, int)
                    or current < previous_count or current > requested_count or current > MAX_OUTPUTS):
                raise RuntimeError("current_count_uncertain")
            actual_modes = self._mode_reader()
            if not isinstance(actual_modes, dict) or len(actual_modes) != current:
                raise RuntimeError("current_endpoint_state_uncertain")
            observed = {key: tuple(mode) for key, mode in actual_modes.items()}
            baseline = {key: tuple(mode) for key, mode in previous_modes.items()}
            if observed == baseline and current == previous_count:
                # The requested change never took effect (or rollback already
                # completed); this exact baseline needs no topology writes.
                verified = True
            else:
                if (not isinstance(expected_endpoint_keys, list)
                        or set(observed) != set(expected_endpoint_keys)):
                    raise RuntimeError("endpoint_identity_changed_before_rollback")
                requested_mode = tuple(PROFILES[profile])
                if (not set(baseline).issubset(observed)
                        or any(observed[key] not in {baseline[key], requested_mode} for key in baseline)
                        or any(key not in baseline and observed[key] not in
                               ({requested_mode} | set(baseline.values())) for key in observed)):
                    raise RuntimeError("mode_state_changed_before_rollback")
                if restore_mode:
                    if self._mode_restorer(previous_modes) is False:
                        raise RuntimeError("mode_restore_rejected")
                    # Restoring baseline modes may race with a hot-plug or an
                    # external display change. Revalidate identities and modes
                    # immediately before the destructive count reduction.
                    restored_count = self._count_reader()
                    restored_modes = self._mode_reader()
                    if (restored_count != current
                            or not isinstance(restored_modes, dict)
                            or set(restored_modes) != set(expected_endpoint_keys)):
                        raise RuntimeError("endpoint_identity_changed_during_mode_restore")
                    restored = {key: tuple(mode) for key, mode in restored_modes.items()}
                    if (any(restored.get(key) != baseline[key] for key in baseline)
                            or any(restored[key] not in {requested_mode, *baseline.values()}
                                   for key in set(restored) - set(baseline))):
                        raise RuntimeError("mode_state_changed_during_mode_restore")
            if restore_count and current != previous_count:
                if self._count_writer(previous_count) is False:
                    raise RuntimeError("count_restore_rejected")
            if not verified:
                if self._count_reader() != previous_count:
                    raise RuntimeError("count_restore_unverified")
                actual_modes = self._mode_reader()
                if (not isinstance(actual_modes, dict)
                        or {key: tuple(value) for key, value in actual_modes.items()} != baseline):
                    raise RuntimeError("mode_restore_unverified")
                verified = True
        except Exception:
            verified = False
        if verified:
            self._session = None
            if not self._save_journal():
                verified = False
        if not verified:
            now = self._clock()
            self._session = {
                "previous_count": previous_count,
                "output_count": requested_count,
                "profile": profile,
                "previous_modes": {key: list(value) for key, value in previous_modes.items()},
                "started_at": now,
                "last_activity": now - IDLE_SECONDS,
                "leases": {},
                "phase": "startup_cleanup_pending",
                "error": str(error)[:200],
                "expected_endpoint_keys": expected_endpoint_keys,
            }
            self._save_journal()

    @_serialized
    def heartbeat(self, lease_id, *, confirmed=False):
        if confirmed is not True:
            raise OutputPoolError("The requester must be explicitly authorized by the manager")
        session = self._require_session()
        if session["phase"] != "active":
            raise OutputPoolError("The output pool is not accepting leases during cleanup")
        lease_id = self._valid_lease_id(lease_id)
        now = self._clock()
        if lease_id not in session["leases"] and len(session["leases"]) >= 128:
            raise OutputPoolError("The output-pool lease limit has been reached")
        previous = session["leases"].get(lease_id)
        previous_activity = session["last_activity"]
        session["leases"][lease_id] = now
        session["last_activity"] = now
        if not self._save_journal():
            if previous is None:
                session["leases"].pop(lease_id, None)
            else:
                session["leases"][lease_id] = previous
            session["last_activity"] = previous_activity
            raise OutputPoolError("Could not persist the output lease heartbeat")
        return self.status(now=now)

    @_serialized
    def release(self, lease_id):
        session = self._require_session()
        lease_id = self._valid_lease_id(lease_id)
        previous = session["leases"].get(lease_id)
        removed = session["leases"].pop(lease_id, None) is not None
        previous_activity = session["last_activity"]
        if removed and not session["leases"]:
            session["last_activity"] = self._clock()
        if removed and not self._save_journal():
            session["leases"][lease_id] = previous
            session["last_activity"] = previous_activity
            raise OutputPoolError("Could not persist output lease release")
        return {"released": removed, **self.status()}

    @_serialized
    def status(self, *, now=None):
        session = self._session
        if session is None:
            if self._persistence_error:
                return {"state": "recovery_required", "idle_seconds": IDLE_SECONDS,
                        "lease_stale_seconds": LEASE_STALE_SECONDS,
                        "leases": 0, "error": self._persistence_error}
            return {"state": "off", "idle_seconds": IDLE_SECONDS,
                    "lease_stale_seconds": LEASE_STALE_SECONDS, "leases": 0}
        now = self._clock() if now is None else now
        state = ("active_unverified" if self._persistence_error else
                 "cleanup_pending" if session["phase"] == "startup_cleanup_pending" else "active")
        return {"state": state,
                "output_count": session["output_count"],
                "previous_count": session["previous_count"], "profile": session["profile"],
                "error": session.get("error"),
                "leases": len(session["leases"]),
                "last_activity": session["last_activity"],
                "idle_remaining_seconds": max(0, IDLE_SECONDS - max(0, now - session["last_activity"])),
                "idle_seconds": IDLE_SECONDS,
                "lease_stale_seconds": LEASE_STALE_SECONDS}

    @_serialized
    def refresh(self):
        """Return the host's standard refresh contract without mutating state."""
        return {**self.status(),
                "module_id": MODULE_ID,
                "module_version": VERSION,
                "max_outputs": MAX_OUTPUTS,
                "profiles": {key: list(value) for key, value in PROFILES.items()},
                "backend_configured": all((self._count_reader, self._count_writer,
                                            self._mode_reader, self._mode_writer,
                                            self._mode_restorer, self._output_window_count))}

    @_serialized
    def task_leaseable_endpoint_keys(self, current_modes):
        """Return only endpoints added by this explicitly confirmed pool session."""
        session = self._session
        if session is None or session.get("phase") != "active" or self._persistence_error:
            return {"eligible": False, "error_code": "OUTPUT_POOL_NOT_VERIFIED"}
        if not isinstance(current_modes, dict):
            return {"eligible": False, "error_code": "OUTPUT_ENDPOINTS_UNAVAILABLE"}
        normalized = {}
        for key, mode in current_modes.items():
            try:
                parse_mode_identity_key(key)
            except OutputPoolError:
                return {"eligible": False, "error_code": "OUTPUT_ENDPOINT_IDENTITY_INVALID"}
            if not isinstance(mode, (list, tuple)) or len(mode) != 3:
                return {"eligible": False, "error_code": "OUTPUT_MODE_UNVERIFIED"}
            normalized[key] = tuple(mode)
        previous = set(session["previous_modes"])
        expected = session.get("expected_endpoint_keys")
        if not previous.issubset(normalized):
            return {"eligible": False, "error_code": "POOL_BASELINE_ENDPOINT_MISSING"}
        expected_new = session["output_count"] - session["previous_count"]
        added = sorted(set(normalized) - previous)
        if expected_new < 1 or len(added) != expected_new:
            return {"eligible": False, "error_code": "NO_POOL_CREATED_ENDPOINT"}
        if not isinstance(expected, list) or set(normalized) != set(expected):
            return {"eligible": False, "error_code": "OUTPUT_ENDPOINT_IDENTITY_UNVERIFIED"}
        plan = PROFILES[session["profile"]]
        if len(normalized) != session["output_count"] or any(mode != plan for mode in normalized.values()):
            return {"eligible": False, "error_code": "POOL_OUTPUT_MODE_MISMATCH"}
        return {"eligible": True, "endpoint_keys": added}

    def _recover_pending_identity_session(self, current_count, current_modes, now):
        """Adopt a confirmed pending pool only when stable endpoint and mode evidence agrees."""
        session = self._session
        if (not isinstance(session, dict) or session.get("phase") != "startup_cleanup_pending"
                or not str(session.get("error") or "").startswith("endpoint_identity:")
                or session.get("expected_endpoint_keys") is not None
                or current_count != session.get("output_count")
                or not isinstance(current_modes, dict) or len(current_modes) != current_count):
            return False
        try:
            requested_mode = tuple(PROFILES[session["profile"]])
            rebound_baseline = rebind_renumbered_endpoints(session["previous_modes"], current_modes)
            observed = {}
            for key, mode in current_modes.items():
                parse_mode_identity_key(key)
                if (not isinstance(mode, (list, tuple)) or len(mode) != 3
                        or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
                               for value in mode)):
                    return False
                observed[key] = tuple(mode)
            if len(rebound_baseline) != session["previous_count"]:
                return False
            baseline_modes = {key: tuple(mode) for key, mode in rebound_baseline.items()}
            if not set(baseline_modes).issubset(observed):
                return False
            if any(observed[key] not in {baseline_modes[key], requested_mode}
                   for key in baseline_modes):
                return False
            if any(mode != requested_mode for key, mode in observed.items()
                   if key not in baseline_modes):
                return False
            if len(observed) - len(baseline_modes) != session["output_count"] - session["previous_count"]:
                return False
            previous = json.loads(json.dumps(session))
            session["previous_modes"] = {key: list(mode) for key, mode in baseline_modes.items()}
            session["expected_endpoint_keys"] = sorted(observed)
            session["phase"] = "active"
            session["error"] = None
            session["last_activity"] = now
            session["leases"] = {}
            if self._save_journal():
                return True
            self._session = previous
            return False
        except (KeyError, TypeError, ValueError, OutputPoolError):
            return False

    def _recover_active_identity_session(self, current_count, current_modes, now):
        """Rebind only an entire active pool whose stable endpoint identities are unchanged."""
        session = self._session
        expected = session.get("expected_endpoint_keys") if isinstance(session, dict) else None
        if (not isinstance(session, dict) or session.get("phase") != "active"
                or current_count != session.get("output_count")
                or not isinstance(expected, list) or len(expected) != current_count
                or len(set(expected)) != len(expected)
                or not isinstance(current_modes, dict) or len(current_modes) != current_count):
            return False
        try:
            if set(expected) == set(current_modes):
                return False
            requested_mode = tuple(PROFILES[session["profile"]])
            # Require every old endpoint (baseline and manager-added) to map to
            # exactly one current endpoint by stable device ID and full monitor path.
            rebound_expected = rebind_renumbered_endpoints(
                {key: [] for key in expected}, current_modes)
            observed = {}
            for key, mode in current_modes.items():
                parse_mode_identity_key(key)
                if (not isinstance(mode, (list, tuple)) or len(mode) != 3
                        or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
                               for value in mode)):
                    return False
                observed[key] = tuple(mode)
            if set(rebound_expected) != set(observed):
                return False
            if any(mode != requested_mode for mode in observed.values()):
                return False
            rebound_baseline = rebind_renumbered_endpoints(session["previous_modes"], current_modes)
            if (not set(rebound_baseline).issubset(observed)
                    or len(observed) - len(rebound_baseline)
                    != session["output_count"] - session["previous_count"]):
                return False
            previous = json.loads(json.dumps(session))
            session["previous_modes"] = rebound_baseline
            session["expected_endpoint_keys"] = sorted(observed)
            session["last_activity"] = now
            if self._save_journal():
                return True
            self._session = previous
            return False
        except (KeyError, TypeError, ValueError, OutputPoolError):
            return False

    @_serialized
    def task_lease_policy(self, current_modes, requested_output_count, profile):
        """Validate the exact manager-confirmed pool before a target session may lease it.

        This does not change display state. Only endpoints created by this active pool
        session are returned; baseline endpoints are never exposed to a task session.
        """
        session = self._session
        if session is None or session.get("phase") != "active" or self._persistence_error:
            return {"eligible": False, "error_code": "OUTPUT_POOL_NOT_VERIFIED"}
        if (isinstance(requested_output_count, bool)
                or not isinstance(requested_output_count, int)
                or requested_output_count < 2
                or not isinstance(profile, str) or profile not in PROFILES):
            return {"eligible": False, "error_code": "OUTPUT_REQUEST_INVALID"}
        if session["profile"] != profile:
            return {"eligible": False, "error_code": "OUTPUT_PROFILE_MISMATCH"}
        if self._count_reader is None:
            return {"eligible": False, "error_code": "OUTPUT_COUNT_UNAVAILABLE"}
        try:
            current_count = self._count_reader()
        except Exception:
            return {"eligible": False, "error_code": "OUTPUT_COUNT_UNAVAILABLE"}
        if session["output_count"] != current_count:
            return {"eligible": False, "error_code": "OUTPUT_COUNT_UNVERIFIED"}
        created_count = session["output_count"] - session["previous_count"]
        if created_count != requested_output_count:
            return {"eligible": False, "error_code": "OUTPUT_COUNT_MISMATCH"}
        result = self.task_leaseable_endpoint_keys(current_modes)
        if result.get("eligible") is not True:
            return result
        keys = result.get("endpoint_keys", [])
        if len(keys) != requested_output_count:
            return {"eligible": False, "error_code": "OUTPUT_ENDPOINT_COUNT_MISMATCH"}
        return {"eligible": True, "endpoint_keys": list(keys),
                "output_count": requested_output_count, "profile": profile,
                "mode": list(PROFILES[profile])}

    @_serialized
    def reconcile(self, *, now=None, force=False, confirmed=False):
        """Restore only the exact manager-created pool after verified idle expiry.

        A live lease or visible output window resets the idle clock and prevents
        restoration. Any uncertain or externally changed topology fails closed.
        `force=True` remains the manager's separately confirmed early-Stop path.
        """
        session = self._session
        if session is None:
            return self.status()
        if self._persistence_error and not force:
            return {**self.status(now=now), "state": "cleanup_unverified", "reason": "recovery_journal_unavailable"}
        if force and confirmed is not True:
            raise OutputPoolError("An explicit in-app confirmation is required to stop early")
        if (self._count_reader is None or self._count_writer is None or self._mode_reader is None
                or self._mode_restorer is None or self._output_window_count is None):
            return {**self.status(), "state": "cleanup_unverified", "reason": "backend_unavailable"}
        now = self._clock() if now is None else now
        stale = [key for key, stamp in session["leases"].items()
                 if now - stamp >= LEASE_STALE_SECONDS]
        for key in stale:
            session["leases"].pop(key, None)
        if stale and not self._save_journal():
            return {**self.status(now=now), "state": "cleanup_unverified", "reason": "recovery_journal_unavailable"}
        try:
            current_count = self._count_reader()
            window_count = self._output_window_count()
            current_modes = self._mode_reader()
        except Exception as exc:
            return {**self.status(now=now), "state": "active_unverified",
                    "reason": f"{type(exc).__name__}: {exc}"[:160]}
        if (isinstance(current_count, bool) or not isinstance(current_count, int)
                or current_count < 0 or current_count > MAX_OUTPUTS
                or isinstance(window_count, bool) or not isinstance(window_count, int) or window_count < 0
                or not isinstance(current_modes, dict) or len(current_modes) != current_count):
            return {**self.status(now=now), "state": "active_unverified", "reason": "unexpected_output_state"}
        baseline_modes = {key: tuple(value) for key, value in session["previous_modes"].items()}
        observed_modes = {key: tuple(value) for key, value in current_modes.items()}
        self._recover_pending_identity_session(current_count, current_modes, now)
        self._recover_active_identity_session(current_count, current_modes, now)
        session = self._session
        baseline_modes = {key: tuple(value) for key, value in session["previous_modes"].items()}
        observed_modes = {key: tuple(value) for key, value in current_modes.items()}
        baseline_is_current = (current_count == session["previous_count"]
                               and observed_modes == baseline_modes)
        if baseline_is_current:
            return self._finish_cleanup(session, now)
        requested_mode = tuple(PROFILES[session["profile"]])
        expected_endpoint_keys = session.get("expected_endpoint_keys")
        identities_match = (isinstance(expected_endpoint_keys, list)
                            and set(observed_modes) == set(expected_endpoint_keys))
        if session["phase"] == "active":
            pool_state_is_current = (identities_match
                                     and current_count == session["output_count"]
                                     and all(mode == requested_mode for mode in observed_modes.values()))
        else:
            # An interrupted startup may be rolled back only when every existing
            # endpoint is still either at its saved baseline or exact requested mode,
            # and any newly added endpoint is already at the exact requested mode.
            previous_keys = set(baseline_modes)
            pool_state_is_current = (identities_match
                                     and session["previous_count"] <= current_count <= session["output_count"]
                                     and previous_keys.issubset(observed_modes)
                                     and all(observed_modes[key] in {baseline_modes[key], requested_mode}
                                             for key in previous_keys)
                                     and all(key in previous_keys or mode == requested_mode
                                             or mode in set(baseline_modes.values())
                                             for key, mode in observed_modes.items()))
        if not pool_state_is_current:
            return {**self.status(now=now), "state": "active_unverified",
                    "reason": "display_configuration_changed_since_pool_request"}
        if session["leases"] or window_count:
            session["last_activity"] = now
            if not self._save_journal():
                return {**self.status(now=now), "state": "active_unverified",
                        "reason": "recovery_journal_unavailable"}
            if force:
                return {**self.status(now=now), "state": "active", "reason": "active_leases_or_windows"}
            return self.status(now=now)
        idle_for = max(0, now - session["last_activity"])
        if not force and idle_for < IDLE_SECONDS and session["phase"] != "startup_cleanup_pending":
            return {**self.status(now=now), "state": "idle_countdown"}
        # At this point there are no active leases/windows and current output
        # count/modes still match the exact pool session. Restore its saved
        # baseline automatically after idle expiry (or for interrupted startup).
        if session["phase"] != "startup_cleanup_pending":
            session["phase"] = "startup_cleanup_pending"
            session["error"] = "idle_cleanup_started" if not force else "confirmed_stop_started"
            if not self._save_journal():
                return {**self.status(now=now), "state": "cleanup_unverified",
                        "reason": "recovery_journal_unavailable"}
        try:
            mode_result = self._mode_restorer(session["previous_modes"])
            if mode_result is False:
                return {**self.status(now=now), "state": "cleanup_unverified", "reason": "baseline_modes_not_restored"}
            # A mode-restoration adapter can race with hot-plug changes. Do
            # not reduce the driver output count until the exact managed set
            # and the restored baseline modes are still present.
            post_restore_count = self._count_reader()
            post_restore_modes = self._mode_reader()
            expected_baseline = {key: tuple(value) for key, value in session["previous_modes"].items()}
            post_restore = ({key: tuple(value) for key, value in post_restore_modes.items()}
                            if isinstance(post_restore_modes, dict) else {})
            if (post_restore_count != session["output_count"]
                    or set(post_restore) != set(expected_endpoint_keys or ())):
                return {**self.status(now=now), "state": "cleanup_unverified",
                        "reason": "endpoint_identity_changed_during_mode_restore"}
            if (any(post_restore.get(key) != value for key, value in expected_baseline.items())
                    or any(post_restore[key] not in {requested_mode, *expected_baseline.values()}
                           for key in set(post_restore) - set(expected_baseline))):
                return {**self.status(now=now), "state": "cleanup_unverified",
                        "reason": "mode_state_changed_during_mode_restore"}
            result = self._count_writer(session["previous_count"])
            if result is False:
                return {**self.status(now=now), "state": "cleanup_unverified", "reason": "driver_no_confirmation"}
            if self._count_reader() != session["previous_count"]:
                return {**self.status(now=now), "state": "cleanup_unverified", "reason": "baseline_count_not_restored"}
            current_modes = self._mode_reader()
            expected = {key: tuple(value) for key, value in session["previous_modes"].items()}
            if (not isinstance(current_modes, dict)
                    or {key: tuple(value) for key, value in current_modes.items()} != expected):
                return {**self.status(now=now), "state": "cleanup_unverified", "reason": "baseline_mode_verification_failed"}
        except Exception as exc:
            return {**self.status(now=now), "state": "cleanup_unverified",
                    "reason": f"{type(exc).__name__}: {exc}"[:160]}
        return self._finish_cleanup(session, now)

    @_serialized
    def retire_detached(self, *, confirmed=False):
        """Explicitly acknowledge prior user removal; never change display topology."""
        if confirmed is not True:
            raise OutputPoolError("Explicit confirmation is required to retire detached outputs")
        session = self._session
        if session is None:
            return self.status()
        if session["leases"]:
            return {**self.status(), "reason": "active_leases"}
        def absent():
            count = self._count_reader()
            modes = self._mode_reader()
            return type(count) is int and count == 0 and isinstance(modes, dict) and not modes
        try:
            if not absent():
                raise OutputPoolError("Outputs are not verified absent")
            if self._persistence is None or not callable(getattr(self._persistence, "archive_retired", None)):
                raise OutputPoolError("Recovery archival is unavailable")
            archive = self._persistence.archive_retired()
            if not isinstance(archive, dict) or not archive.get("sha256"):
                raise OutputPoolError("Recovery archival was not verified")
            if not absent():
                raise OutputPoolError("Outputs changed during recovery archival")
        except Exception as exc:
            return {**self.status(), "state": "cleanup_unverified", "reason": type(exc).__name__}
        self._session = None
        if not self._save_journal():
            self._session = session
            return {**self.status(), "state": "cleanup_unverified", "reason": "journal_clear_failed"}
        return {"state": "off", "leases": 0, "outputs_absent": True, "recovery_archive": archive}

    def _finish_cleanup(self, session, now):
        self._session = None
        if not self._save_journal():
            self._session = session
            session["phase"] = "startup_cleanup_pending"
            session["last_activity"] = now - IDLE_SECONDS
            session["error"] = "baseline_restored_but_journal_clear_failed"
            return {**self.status(now=now), "state": "cleanup_unverified",
                    "reason": "recovery_journal_clear_failed"}
        return {"state": "off", "expired": True, "restored_count": session["previous_count"],
                "idle_seconds": IDLE_SECONDS, "leases": 0}

    def _require_session(self):
        if self._session is None:
            raise OutputPoolError("No output-pool session is active")
        return self._session

    @staticmethod
    def _valid_lease_id(value):
        if not isinstance(value, str) or not value or len(value) > 128 or any(ord(c) < 32 for c in value):
            raise OutputPoolError("Invalid output-pool lease ID")
        return value

    @_serialized
    def dispose(self):
        # An active display session prevents module reload/disposal. The host
        # must keep the visible manager resident until cleanup is verified.
        if self._session is not None:
            raise RuntimeError("An active output-pool session prevents disposal")
        self._disposed = True

    @_serialized
    def reload_blocked(self):
        return self._session is not None

    def action(self, action, payload):
        if action == "pool_status":
            return self.status()
        if action == "pool_heartbeat":
            return self.heartbeat(payload.get("lease_id"), confirmed=payload.get("authorized") is True)
        if action == "pool_release":
            return self.release(payload.get("lease_id"))
        if action == "pool_reconcile":
            return self.reconcile()
        if action == "pool_stop":
            return self.reconcile(force=True, confirmed=payload.get("confirmed") is True)
        if action == "pool_retire_detached":
            return self.retire_detached(confirmed=payload.get("confirmed") is True)
        if action == "pool_begin":
            return self.begin(payload.get("output_count"), payload.get("profile"),
                              payload.get("supported_modes", ()),
                              confirmed=payload.get("confirmed") is True)
        raise ValueError("Unknown virtual output pool action")


ACTIONS = ("pool_status", "pool_heartbeat", "pool_release", "pool_reconcile", "pool_stop", "pool_begin", "pool_retire_detached")


def create():
    return OutputPoolModule()
