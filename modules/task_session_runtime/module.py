"""Fail-closed task-scoped runtime contract; no target backend is enabled by default."""
from __future__ import annotations

import math
import hashlib
import os
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from core.private_capture_codes import PRIVATE_DESKTOP_CAPTURE_DIAGNOSTIC_CODES
from core.runtime_packet import PROTOCOL_VERSION as PACKET_PROTOCOL_VERSION
from core.runtime_packet import encode_runtime_packet
from modules.virtual_output_pool.module import MAX_OUTPUTS, PROFILES

API_VERSION = 1
MODULE_ID = "task_session_runtime"
VERSION = "0.1.22"
ACTIONS = ("status", "start", "observe", "act", "interrupt", "heartbeat", "cancel", "stop")
CONCURRENT_ACTIONS = ("status", "interrupt", "cancel", "stop")
PROTOCOL_VERSION = 2
IDLE_TIMEOUT_SECONDS = 300
BACKEND_TIMEOUT_SECONDS = 15
DEFAULT_WATCHDOG_INTERVAL_SECONDS = 1.0
MAX_WATCHDOG_INTERVAL_SECONDS = 60.0
# 65,536 is over 7.5 days at one stateful request every 10 seconds. The
# fail-closed ceiling remains finite while not expiring an ordinary work session.
MAX_STATEFUL_REQUESTS = 65536
STATEFUL_ACTIONS = frozenset({"start", "observe", "act", "interrupt", "heartbeat"})
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)


def _valid_window_class_pattern(value: str) -> bool:
    """Allow exact classes or one bounded trailing-star prefix pattern."""
    if not isinstance(value, str) or not value.strip() or "\x00" in value or len(value) > 128:
        return False
    if "*" not in value:
        return True
    if not value.endswith("*") or value.count("*") != 1:
        return False
    prefix = value[:-1]
    return (len(prefix) >= 12 and prefix.isascii()
            and all(char.isalnum() or char in "._-" for char in prefix))
APP_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
ACTION_ID_RE = APP_ID_RE
INTERRUPT_REASONS = frozenset({"host_input_desktop_changed", "host_input_desktop_unknown",
    "target_lost", "capture_failed", "policy_prompt_required", "host_disconnected"})


@dataclass(frozen=True)
class TargetActionDescriptor:
    """One exact semantic UIA action with a required observable postcondition."""
    action_id: str
    control_type: str
    control_name: str
    postcondition_name: str
    postcondition_value: str

    def __post_init__(self):
        if not isinstance(self.action_id, str) or not ACTION_ID_RE.fullmatch(self.action_id):
            raise ValueError("Invalid target action identifier")
        if self.control_type not in {"Button", "MenuItem", "TabItem", "CheckBox", "RadioButton"}:
            raise ValueError("Unsupported semantic control type")
        for field_name in ("control_name", "postcondition_name", "postcondition_value"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip() or len(value) > 256 or "\x00" in value:
                raise ValueError(f"Invalid target action {field_name}")


@dataclass(frozen=True)
class OutputRequest:
    """Bounded task request for manager-created output count and quality."""
    output_count: int
    profile: str

    @classmethod
    def from_payload(cls, value):
        if not isinstance(value, dict) or set(value) != {"output_count", "profile"}:
            raise ValueError("Output request fields are invalid")
        count, profile = value["output_count"], value["profile"]
        if type(count) is int and count == 0 and profile == "private":
            return cls(0, "private")
        if (isinstance(count, bool) or not isinstance(count, int)
                or not 2 <= count <= MAX_OUTPUTS):
            raise ValueError("Output count must be an integer within the manager limit")
        if not isinstance(profile, str) or profile not in PROFILES:
            raise ValueError("Output profile is not supported by the manager")
        return cls(count, profile)

    def as_dict(self):
        return {"output_count": self.output_count, "profile": self.profile}


@dataclass(frozen=True)
class TargetLaunchDescriptor:
    """Immutable manager-owned launch identity; requests may select only its app_id."""
    executable_path: str
    executable_sha256: str
    arguments: tuple[str, ...]
    working_directory: str
    window_class_names: tuple[str, ...]
    actions: tuple[TargetActionDescriptor, ...]
    capture_policy: str = "private_desktop_exact_hwnd"
    cleanup_timeout_seconds: int = 5
    maximum_session_seconds: int = 3600

    def __post_init__(self):
        if (not isinstance(self.executable_path, str) or not self.executable_path
                or not Path(self.executable_path).is_absolute()):
            raise ValueError("Target executable path must be absolute")
        if (not isinstance(self.working_directory, str) or not self.working_directory
                or not Path(self.working_directory).is_absolute()):
            raise ValueError("Target working directory must be absolute")
        if not isinstance(self.executable_sha256, str) or not re.fullmatch(
                r"[0-9a-f]{64}", self.executable_sha256):
            raise ValueError("Target executable SHA-256 must be lowercase hexadecimal")
        if (not isinstance(self.arguments, tuple) or len(self.arguments) > 64
                or any(not isinstance(item, str) or "\x00" in item or len(item) > 2048
                       for item in self.arguments)
                or sum(len(item) for item in self.arguments) > 8192):
            raise ValueError("Target arguments must be a bounded immutable tuple")
        if (not isinstance(self.window_class_names, tuple)
                or not 1 <= len(self.window_class_names) <= 16
                or any(not _valid_window_class_pattern(item)
                       for item in self.window_class_names)):
            raise ValueError("At least one bounded exact or prefix target window class is required")
        if (not isinstance(self.actions, tuple) or len(self.actions) > 64
                or any(not isinstance(item, TargetActionDescriptor) for item in self.actions)
                or len({item.action_id for item in self.actions}) != len(self.actions)):
            raise ValueError("Target actions must be a unique immutable descriptor tuple")
        if self.capture_policy != "private_desktop_exact_hwnd":
            raise ValueError("Unsupported isolated target capture policy")
        for name, value, maximum in (("cleanup timeout", self.cleanup_timeout_seconds, 60),
                                     ("maximum session", self.maximum_session_seconds, 86400)):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"Invalid target {name}")

    @property
    def action_ids(self) -> frozenset[str]:
        return frozenset(item.action_id for item in self.actions)


def verify_target_executable_identity(descriptor: TargetLaunchDescriptor) -> dict:
    """Hash the exact configured image immediately before launch; never trust path text alone."""
    if not isinstance(descriptor, TargetLaunchDescriptor):
        return {"verified": False, "error_code": "TARGET_DESCRIPTOR_INVALID"}
    try:
        path = Path(descriptor.executable_path)
        resolved = path.resolve(strict=True)
        if (not resolved.is_file() or not path.samefile(resolved)):
            return {"verified": False, "error_code": "TARGET_IMAGE_PATH_CHANGED"}
        digest = hashlib.sha256()
        with resolved.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        actual = digest.hexdigest()
        if actual != descriptor.executable_sha256:
            return {"verified": False, "error_code": "TARGET_IMAGE_HASH_MISMATCH"}
        return {"verified": True, "error_code": None, "sha256": actual}
    except (OSError, RuntimeError, TypeError, ValueError):
        return {"verified": False, "error_code": "TARGET_IMAGE_UNAVAILABLE"}


@dataclass(frozen=True)
class TargetSpec:
    """Manager-owned allowlist entry. The consumer may select an ID, never a path."""
    app_id: str
    action_ids: frozenset[str]
    launch: TargetLaunchDescriptor | None = None
    minimum_output_count: int = 2
    maximum_output_count: int = MAX_OUTPUTS
    output_profiles: frozenset[str] = frozenset({"high", "ultra"})
    isolation_mode: str = "host_vdd"

    @property
    def requires_output_lease(self):
        return self.launch is not None and self.isolation_mode == "host_vdd"

    def __post_init__(self):
        if self.isolation_mode == "private_desktop":
            if (type(self.minimum_output_count) is not int or self.minimum_output_count != 0
                    or type(self.maximum_output_count) is not int or self.maximum_output_count != 0
                    or self.output_profiles != frozenset({"private"}) or self.launch is None):
                raise ValueError("Private desktop targets require an exact zero-output launch policy")
            return
        if self.isolation_mode != "host_vdd":
            raise ValueError("Unsupported target isolation mode")
        if (isinstance(self.minimum_output_count, bool)
                or not isinstance(self.minimum_output_count, int)
                or not 2 <= self.minimum_output_count <= MAX_OUTPUTS):
            raise ValueError("Target minimum output count is invalid")
        if (isinstance(self.maximum_output_count, bool)
                or not isinstance(self.maximum_output_count, int)
                or not self.minimum_output_count <= self.maximum_output_count <= MAX_OUTPUTS):
            raise ValueError("Target maximum output count is invalid")
        if (not isinstance(self.output_profiles, frozenset)
                or not self.output_profiles
                or not self.output_profiles.issubset(PROFILES.keys())):
            raise ValueError("Target output profiles must be an immutable supported-profile set")


@dataclass(frozen=True)
class TargetHandle:
    """Backend identity proof required before a session can become ready."""
    opaque: Any
    backend: str
    process_id: int
    window_id: int
    job_owned: bool
    window_absent_from_input_desktop: bool
    input_desktop_unchanged: bool

    @property
    def ready_verified(self) -> bool:
        return (isinstance(self.backend, str) and bool(self.backend)
                and type(self.process_id) is int and self.process_id > 0
                and type(self.window_id) is int and self.window_id > 0
                and self.job_owned is True
                and self.window_absent_from_input_desktop is True
                and self.input_desktop_unchanged is True)


@dataclass(frozen=True)
class Observation:
    png: bytes
    accessibility: dict


@dataclass(frozen=True)
class StopReceipt:
    stopped: bool
    target_stopped: bool
    job_closed: bool
    desktop_removed: bool
    input_desktop_unchanged: bool
    output_lease_released: bool = True
    profile_data_removed: bool = True

    @property
    def verified(self) -> bool:
        return all(value is True for value in (
            self.stopped, self.target_stopped, self.job_closed,
            self.desktop_removed, self.input_desktop_unchanged,
            self.output_lease_released, self.profile_data_removed))


class BackendStartError(RuntimeError):
    """Backend launch failure with explicit evidence about any partial target."""
    def __init__(self, error_code="TARGET_START_FAILED", *, target_created=None,
                 handle=None, cleanup_receipt=None):
        super().__init__(error_code)
        self.error_code = error_code
        self.target_created = target_created
        self.handle = handle
        self.cleanup_receipt = cleanup_receipt


class BackendOperationError(RuntimeError):
    """Backend failure with an optional stable runtime interruption reason."""
    def __init__(self, message="BACKEND_OPERATION_FAILED", *, interrupt_reason=None,
                 diagnostic_code=None):
        super().__init__(message)
        self.interrupt_reason = interrupt_reason
        self.diagnostic_code = diagnostic_code


@dataclass
class _Session:
    session_id: str
    target: TargetSpec
    state: str = "starting"
    handle: TargetHandle | None = None
    created_at: float = 0.0
    last_activity: float = 0.0
    interrupt_reason: str | None = None
    cancel_requested: bool = False
    receipt: StopReceipt | None = None
    error_code: str | None = None
    output_lease_id: str | None = None
    output_endpoint_keys: tuple[str, ...] = ()


class TaskOutputLeaseCoordinator:
    """Lease only the exact new MTT endpoints in an already-confirmed pool."""

    def __init__(self, pool, topology_reader):
        if not callable(getattr(pool, "task_lease_policy", None)):
            raise TypeError("The manager output pool has no task lease policy")
        if not callable(topology_reader):
            raise TypeError("A verified live topology reader is required")
        self._pool = pool
        self._topology_reader = topology_reader
        self._leases: dict[str, dict] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _mode_snapshot(topology):
        if (not isinstance(topology, dict) or topology.get("topology_verified") is not True
                or not isinstance(topology.get("displays"), list)):
            return None, "OUTPUT_TOPOLOGY_UNVERIFIED"
        parent = str(topology.get("mtt_device_instance_id", "")).strip().casefold()
        if not parent:
            return None, "OUTPUT_DEVICE_IDENTITY_UNVERIFIED"
        from modules.virtual_output_pool.module import mode_identity_key
        modes = {}
        for row in topology["displays"]:
            if not isinstance(row, dict):
                return None, "OUTPUT_TOPOLOGY_INVALID"
            if str(row.get("monitor_parent", "")).strip().casefold() != parent:
                continue
            if (row.get("virtual") is not True or row.get("attached") is not True
                    or row.get("monitor_identity_verified") is not True
                    or row.get("topology_verified") is not True
                    or row.get("verified") is not True or row.get("cloned") is not False
                    or row.get("clone_source_count") != 1):
                return None, "OUTPUT_ENDPOINT_NOT_ISOLATED"
            try:
                key = mode_identity_key(row.get("display_name"),
                                        row.get("monitor_device_id"), row.get("monitor_path"))
                dims = (row.get("width"), row.get("height"), row.get("refresh_rate"))
                if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
                       for value in dims):
                    raise ValueError("invalid mode")
            except (TypeError, ValueError):
                return None, "OUTPUT_ENDPOINT_IDENTITY_INVALID"
            if key in modes:
                return None, "OUTPUT_ENDPOINT_AMBIGUOUS"
            modes[key] = dims
        if not modes:
            return None, "OUTPUT_ENDPOINTS_UNAVAILABLE"
        return modes, None

    def acquire(self, lease_id: str, output_request: OutputRequest):
        with self._lock:
            if lease_id in self._leases:
                return {"eligible": False, "error_code": "OUTPUT_LEASE_ALREADY_EXISTS"}
            try:
                modes, error = self._mode_snapshot(self._topology_reader())
                if error:
                    return {"eligible": False, "error_code": error}
                policy = self._pool.task_lease_policy(
                    modes, output_request.output_count, output_request.profile)
                if not isinstance(policy, dict) or policy.get("eligible") is not True:
                    return {"eligible": False,
                            "error_code": (policy.get("error_code") if isinstance(policy, dict)
                                           else "OUTPUT_POOL_POLICY_INVALID")}
                endpoints = policy.get("endpoint_keys")
                if (not isinstance(endpoints, list) or len(endpoints) != output_request.output_count
                        or len(set(endpoints)) != len(endpoints)
                        or any(key not in modes for key in endpoints)):
                    return {"eligible": False, "error_code": "OUTPUT_ENDPOINT_SET_INVALID"}
                state = self._pool.heartbeat(lease_id, confirmed=True)
                self._leases[lease_id] = {
                    "endpoint_keys": tuple(endpoints),
                    "output_count": output_request.output_count,
                    "profile": output_request.profile,
                }
                if (not isinstance(state, dict) or state.get("state") != "active"
                        or state.get("output_count", 0) - state.get("previous_count", 0)
                        != output_request.output_count
                        or state.get("profile") != output_request.profile):
                    released = self.release(lease_id)
                    return {"eligible": False, "error_code": (
                        "OUTPUT_LEASE_NOT_VERIFIED" if released
                        else "OUTPUT_LEASE_RELEASE_UNVERIFIED"),
                        "lease_retained": not released}
                # Re-read identity after the journaled lease commit. A changed
                # endpoint is released before any target launch is attempted.
                fresh_modes, fresh_error = self._mode_snapshot(self._topology_reader())
                fresh_policy = (self._pool.task_lease_policy(
                    fresh_modes, output_request.output_count, output_request.profile)
                    if fresh_error is None else {})
                if (fresh_error is not None or fresh_policy.get("eligible") is not True
                        or tuple(fresh_policy.get("endpoint_keys", ())) != tuple(endpoints)):
                    released = self.release(lease_id)
                    return {"eligible": False, "error_code": (
                        "OUTPUT_TOPOLOGY_CHANGED" if released
                        else "OUTPUT_LEASE_RELEASE_UNVERIFIED"),
                        "lease_retained": not released}
                return {"eligible": True, "endpoint_keys": list(endpoints),
                        "output_count": output_request.output_count,
                        "profile": output_request.profile}
            except Exception:
                retained = lease_id in self._leases
                if retained:
                    retained = not self.release(lease_id)
                return {"eligible": False,
                        "error_code": "OUTPUT_LEASE_RELEASE_UNVERIFIED" if retained
                        else "OUTPUT_LEASE_UNAVAILABLE",
                        "lease_retained": retained}

    def readiness(self, target: "TargetSpec") -> dict:
        """Read-only check that the current pool can satisfy this launch target."""
        if not isinstance(target, TargetSpec) or target.launch is None:
            return {"eligible": False, "error_code": "TARGET_LAUNCH_POLICY_REQUIRED"}
        try:
            state = self._pool.status()
            if not isinstance(state, dict) or state.get("state") not in {"active", "idle_countdown"}:
                return {"eligible": False, "error_code": "OUTPUT_POOL_NOT_VERIFIED"}
            total = state.get("output_count")
            previous = state.get("previous_count")
            profile = state.get("profile")
            if (isinstance(total, bool) or not isinstance(total, int)
                    or isinstance(previous, bool) or not isinstance(previous, int)
                    or not 0 <= previous < total):
                return {"eligible": False, "error_code": "OUTPUT_POOL_STATE_INVALID"}
            created = total - previous
            if not target.minimum_output_count <= created <= target.maximum_output_count:
                return {"eligible": False, "error_code": "OUTPUT_COUNT_MISMATCH"}
            if not isinstance(profile, str) or profile not in target.output_profiles:
                return {"eligible": False, "error_code": "OUTPUT_PROFILE_MISMATCH"}
            modes, error = self._mode_snapshot(self._topology_reader())
            if error is not None:
                return {"eligible": False, "error_code": error}
            policy = self._pool.task_lease_policy(modes, created, profile)
            if not isinstance(policy, dict) or policy.get("eligible") is not True:
                return {"eligible": False,
                        "error_code": (policy.get("error_code") if isinstance(policy, dict)
                                       else "OUTPUT_POOL_POLICY_INVALID")}
            keys = policy.get("endpoint_keys")
            if (not isinstance(keys, list) or len(keys) != created
                    or len(set(keys)) != len(keys)
                    or any(key not in modes for key in keys)):
                return {"eligible": False, "error_code": "OUTPUT_ENDPOINT_SET_INVALID"}
            return {"eligible": True, "output_count": created, "profile": profile}
        except Exception:
            return {"eligible": False, "error_code": "OUTPUT_POOL_READINESS_UNAVAILABLE"}

    def heartbeat(self, lease_id: str) -> bool:
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                return False
            try:
                modes, error = self._mode_snapshot(self._topology_reader())
                if error is not None:
                    return False
                policy = self._pool.task_lease_policy(
                    modes, lease["output_count"], lease["profile"])
                if (not isinstance(policy, dict) or policy.get("eligible") is not True
                        or tuple(policy.get("endpoint_keys", ())) != lease["endpoint_keys"]):
                    return False
                state = self._pool.heartbeat(lease_id, confirmed=True)
                return (isinstance(state, dict) and state.get("state") == "active"
                        and state.get("output_count", 0) - state.get("previous_count", 0)
                        == lease["output_count"]
                        and state.get("profile") == lease["profile"])
            except Exception:
                return False

    def release(self, lease_id: str) -> bool:
        with self._lock:
            if lease_id not in self._leases:
                return False
            try:
                result = self._pool.release(lease_id)
                if not isinstance(result, dict) or result.get("released") is not True:
                    status = self._pool.status()
                    if not isinstance(status, dict) or status.get("state") != "off":
                        return False
            except Exception:
                return False
            self._leases.pop(lease_id, None)
            return True


class TaskSessionRuntime:
    """Versioned lifecycle dispatcher behind a trusted manager-owned target registry.

    The default instance has no targets and no backend. Concrete backends must create
    the exact allowlisted process on a private desktop, return identity proof, provide
    bounded observe/act calls, and return a complete stop receipt.
    """

    _FIELDS = {
        "status": {"protocol_version", "request_id", "action"},
        "start": {"protocol_version", "request_id", "action", "target_app_id", "confirmed",
                  "output_request"},
        "observe": {"protocol_version", "request_id", "action", "session_id"},
        "act": {"protocol_version", "request_id", "action", "session_id", "action_id"},
        "interrupt": {"protocol_version", "request_id", "action", "session_id", "reason"},
        "heartbeat": {"protocol_version", "request_id", "action", "session_id"},
        "cancel": {"protocol_version", "request_id", "action", "session_id"},
        "stop": {"protocol_version", "request_id", "action", "session_id"},
    }

    def __init__(self, backend=None, target_registry: Mapping[str, TargetSpec] | None = None,
                 output_lease: TaskOutputLeaseCoordinator | None = None,
                 clock=time.monotonic,
                 watchdog_interval: float = DEFAULT_WATCHDOG_INTERVAL_SECONDS):
        self._backend = backend
        self._owner_runtimes = {}
        self._owner_saved_states = {}
        self._owners_disposing = False
        self._output_lease = output_lease
        self._targets = self._validate_registry(target_registry or {})
        self._runtime_configured = backend is not None or bool(self._targets)
        self._target_registry_error = None
        self._clock = clock
        if (isinstance(watchdog_interval, bool)
                or not isinstance(watchdog_interval, (int, float))):
            raise ValueError("watchdog_interval must be a finite number between 0 and 60 seconds")
        try:
            interval = float(watchdog_interval)
        except (OverflowError, ValueError):
            raise ValueError("watchdog_interval must be a finite number between 0 and 60 seconds") from None
        if (not math.isfinite(interval) or interval < 0
                or interval > MAX_WATCHDOG_INTERVAL_SECONDS):
            raise ValueError("watchdog_interval must be a finite number between 0 and 60 seconds")
        self._watchdog_interval = interval
        self._lock = threading.RLock()
        self._operation_lock = threading.RLock()
        self._initialized = False
        self._active: _Session | None = None
        self._receipts: dict[str, StopReceipt] = {}
        self._seen_request_ids: set[str] = set()
        self._seen_stateful_request_ids: set[str] = set()
        self._request_order: deque[str] = deque()
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: threading.Thread | None = None

    @staticmethod
    def _validate_registry(registry: Mapping[str, TargetSpec]) -> dict[str, TargetSpec]:
        if not isinstance(registry, Mapping):
            raise TypeError("Target registry must be manager-owned mapping data")
        clean = {}
        for app_id, spec in registry.items():
            if (not isinstance(app_id, str) or not APP_ID_RE.fullmatch(app_id)
                    or not isinstance(spec, TargetSpec) or spec.app_id != app_id
                    or not isinstance(spec.action_ids, frozenset)
                    or any(not isinstance(action, str) or not ACTION_ID_RE.fullmatch(action)
                           for action in spec.action_ids)
                    or (spec.launch is not None and (
                        not isinstance(spec.launch, TargetLaunchDescriptor)
                        or spec.launch.action_ids != spec.action_ids))):
                raise ValueError("Invalid manager-owned target registry entry")
            clean[app_id] = spec
        return clean

    def init(self):
        with self._lock:
            if self._initialized:
                raise RuntimeError("Task session runtime already initialized")
            self._initialized = True
            self._watchdog_stop.clear()
            if self._backend is not None and self._watchdog_interval > 0:
                self._watchdog_thread = threading.Thread(
                    target=self._watchdog_loop, name="task-session-idle-watchdog", daemon=True)
                try:
                    self._watchdog_thread.start()
                except Exception:
                    self._watchdog_thread = None
                    self._initialized = False
                    raise

    def bind_output_lease(self, output_lease: TaskOutputLeaseCoordinator):
        if not isinstance(output_lease, TaskOutputLeaseCoordinator):
            raise TypeError("A manager-owned task output lease coordinator is required")
        with self._lock:
            if self._active is not None:
                raise RuntimeError("Cannot bind outputs while a task session is active")
            self._output_lease = output_lease

    def configure_backend(self, backend, target_registry: Mapping[str, TargetSpec],
                          *, target_registry_error: str | None = None):
        """Bind one manager-owned backend/catalog before the authenticated broker starts."""
        if backend is None or not callable(getattr(backend, "start", None)):
            raise TypeError("A task-session backend is required")
        targets = self._validate_registry(target_registry)
        with self._lock:
            if not self._initialized:
                raise RuntimeError("Task session runtime must be initialized before configuration")
            if (self._runtime_configured or self._active is not None
                    or self._seen_request_ids):
                raise RuntimeError("Task session runtime configuration is already locked")
            self._backend = backend
            self._targets = targets
            self._target_registry_error = target_registry_error
            self._runtime_configured = True
            if self._watchdog_interval > 0:
                self._watchdog_stop.clear()
                self._watchdog_thread = threading.Thread(
                    target=self._watchdog_loop, name="task-session-idle-watchdog", daemon=True)
                try:
                    self._watchdog_thread.start()
                except Exception:
                    self._watchdog_thread = None
                    self._backend = None
                    self._targets = {}
                    self._target_registry_error = None
                    self._runtime_configured = False
                    raise

    def _watchdog_loop(self):
        while not self._watchdog_stop.wait(self._watchdog_interval):
            try:
                now = self._clock()
                with self._lock:
                    session = self._active
                    if (session is None or session.handle is None
                            or session.cancel_requested
                            or now - session.last_activity <= IDLE_TIMEOUT_SECONDS):
                        continue
                    session.cancel_requested = True
                    session.state = "stopping"
                with self._operation_lock:
                    with self._lock:
                        if self._active is not session or session.handle is None:
                            continue
                        handle = session.handle
                    self._stop_handle(session, handle)
            except Exception:
                # Never turn a monitor exception into an unverified cleanup claim.
                continue

    def snapshot(self):
        with self._lock:
            owner_states = dict(self._owner_saved_states)
            owner_states.update({owner: child.snapshot()
                                 for owner, child in self._owner_runtimes.items()})
            if self._active is not None:
                raise RuntimeError("Task session is active; module reload is deferred")
            return {"state": "idle",
                    "stateful_request_ids": sorted(self._seen_stateful_request_ids),
                    "receipts": {}, "owner_states": owner_states}

    def restore(self, state):
        if state is None:
            return
        if not isinstance(state, dict) or state.get("state") != "idle":
            raise RuntimeError("Active task sessions cannot be restored across module reload")
        owners = state.get("owner_states", {})
        if not isinstance(owners, dict) or len(owners) > 8:
            raise ValueError("Invalid owner snapshots")
        for owner, snapshot in owners.items():
            if (not isinstance(owner, str) or not 1 <= len(owner) <= 128
                    or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for c in owner)
                    or not isinstance(snapshot, dict) or snapshot.get("owner_states")):
                raise ValueError("Invalid owner snapshot")
            probe = TaskSessionRuntime(watchdog_interval=0)
            probe.restore(snapshot)
        self._owner_saved_states = dict(owners)
        request_ids = state.get("stateful_request_ids", [])
        if (not isinstance(request_ids, list)
                or len(request_ids) > MAX_STATEFUL_REQUESTS
                or any(not isinstance(item, str) or not UUID_RE.fullmatch(item)
                       for item in request_ids)
                or len(set(request_ids)) != len(request_ids)):
            raise ValueError("Invalid stateful request ledger in module snapshot")
        with self._lock:
            if self._initialized or self._active is not None:
                raise RuntimeError("Cannot restore into an initialized or active runtime")
            self._seen_stateful_request_ids = set(request_ids)

    def refresh(self):
        with self._lock:
            active = self._active
            owner_states = {owner: child.refresh()
                            for owner, child in self._owner_runtimes.items()}
            active_owner = next((state for state in owner_states.values()
                                 if state.get("active_session_id")), None)
            policies = []
            for target in sorted(self._targets.values(), key=lambda item: item.app_id):
                lease_required = target.requires_output_lease
                lease_check = ({"eligible": False,
                                "error_code": "OUTPUT_LEASE_COORDINATOR_UNAVAILABLE"}
                               if lease_required and self._output_lease is None else
                               self._output_lease.readiness(target)
                               if lease_required else {"eligible": True, "error_code": None})
                policies.append({
                    "target_app_id": target.app_id,
                    **({"isolation_mode": target.isolation_mode}
                       if target.isolation_mode == "private_desktop" else {}),
                    "minimum_output_count": target.minimum_output_count,
                    "maximum_output_count": target.maximum_output_count,
                    "output_profiles": sorted(target.output_profiles),
                    "output_lease_required": lease_required,
                    "output_lease_ready": lease_check.get("eligible") is True,
                    "output_lease_error": lease_check.get("error_code"),
                    "available_output_count": lease_check.get("output_count"),
                    "available_output_profile": lease_check.get("profile"),
                })
            backend_available = self._backend is not None
            output_lease_bound = self._output_lease is not None
            output_lease_required = any(item["output_lease_required"] for item in policies)
            readiness_error = None
            if self._target_registry_error:
                readiness_error = self._target_registry_error
            elif not backend_available:
                readiness_error = "ISOLATED_BACKEND_UNAVAILABLE"
            elif not policies:
                readiness_error = "TARGET_ALLOWLIST_EMPTY"
            elif output_lease_required and not output_lease_bound:
                readiness_error = "OUTPUT_LEASE_COORDINATOR_UNAVAILABLE"
            elif (output_lease_required and policies
                  and not any(not item["output_lease_required"]
                              or item["output_lease_ready"] for item in policies)):
                readiness_error = "OUTPUT_POOL_NOT_READY"
            return {"protocol_version": PROTOCOL_VERSION,
                    "backend_available": backend_available,
                    "target_count": len(self._targets),
                    "target_policies": policies,
                    "active_session_id": (active.session_id if active else
                                          active_owner["active_session_id"] if active_owner else None),
                    "session_state": (active.state if active else
                                      active_owner["session_state"] if active_owner else "stopped"),
                    "owner_sessions": owner_states,
                    "owner_capacity": 8,
                    "consumer_connected": False,
                    "output_lease_bound": output_lease_bound,
                    "output_lease_required": output_lease_required,
                    "output_lease_ready": any(
                        not item["output_lease_required"] or item["output_lease_ready"]
                        for item in policies),
                    "readiness_error": readiness_error,
                    "lease_acquisition": "fail_closed"}

    def action(self, name, payload):
        if not isinstance(payload, dict):
            return self._reply({}, "failed", "REQUEST_INVALID")
        request = dict(payload)
        request["action"] = name
        return self.handle_request(request)

    def handle_request(self, request):
        if not isinstance(request, dict):
            return self._reply({}, "failed", "REQUEST_INVALID")
        if "owner_id" in request:
            return self._handle_owner_request(request)
        action = request.get("action")
        request_id = request.get("request_id")
        if (type(request.get("protocol_version")) is not int
                or request.get("protocol_version") != PROTOCOL_VERSION
                or not isinstance(action, str) or action not in self._FIELDS
                or set(request) != self._FIELDS[action]
                or not isinstance(request_id, str) or not UUID_RE.fullmatch(request_id)):
            return self._reply(request, "failed", "REQUEST_INVALID")
        with self._lock:
            if not self._initialized:
                return self._reply(request, "failed", "RUNTIME_NOT_INITIALIZED")
            if (request_id in self._seen_request_ids
                    or request_id in self._seen_stateful_request_ids):
                return self._reply(request, "failed", "REQUEST_REPLAYED")
            if (action in STATEFUL_ACTIONS
                    and len(self._seen_stateful_request_ids) >= MAX_STATEFUL_REQUESTS):
                return self._reply(request, "failed", "REQUEST_LIMIT_REACHED")
            self._seen_request_ids.add(request_id)
            self._request_order.append(request_id)
            if action in STATEFUL_ACTIONS:
                # Retain every state-changing request for this module lifetime so
                # cache churn cannot make an old start/act/heartbeat replayable.
                self._seen_stateful_request_ids.add(request_id)
            if len(self._request_order) > 1024:
                self._seen_request_ids.discard(self._request_order.popleft())

        if action == "status":
            state = self.refresh()
            readiness_error = state["readiness_error"]
            return self._reply(request, "running" if readiness_error is None else "failed",
                               readiness_error,
                               extra={"target_count": state["target_count"],
                                      "active_session_id": state["active_session_id"],
                                      "session_state": state["session_state"],
                                      "target_policies": state["target_policies"],
                                      "owner_sessions": state["owner_sessions"],
                                      "owner_capacity": state["owner_capacity"],
                                      "consumer_connected": False,
                                      "output_lease_bound": state["output_lease_bound"],
                                      "output_lease_required": state["output_lease_required"],
                                      "output_lease_ready": state["output_lease_ready"],
                                      "readiness_error": state["readiness_error"],
                                      "target_policies": state["target_policies"],
                                      "lease_acquisition": "fail_closed"})
        if action == "start":
            return self._start(request)
        if action == "observe":
            return self._observe(request)
        if action == "act":
            return self._act(request)
        if action == "interrupt":
            return self._interrupt(request)
        if action == "heartbeat":
            return self._heartbeat(request)
        return self._cancel(request)

    @staticmethod
    def _reply(request, state, error_code=None, session_id=None, extra=None):
        result = {"protocol_version": PROTOCOL_VERSION,
                  "request_id": request.get("request_id"),
                  "session_id": session_id,
                  "state": state,
                  "error_code": error_code}
        if extra:
            result.update(extra)
        return result

    def _session(self, request):
        session_id = request.get("session_id")
        if not isinstance(session_id, str) or not UUID_RE.fullmatch(session_id):
            return None, self._reply(request, "failed", "SESSION_ID_INVALID")
        with self._lock:
            active = self._active
            if active and active.session_id == session_id:
                return active, None
            receipt = self._receipts.get(session_id)
        if receipt is not None:
            return None, self._reply(request, "stopped", "SESSION_ALREADY_STOPPED", session_id,
                                     {"stop_receipt": receipt.__dict__})
        return None, self._reply(request, "failed", "SESSION_NOT_FOUND", session_id)

    def _start(self, request):
        target_id = request.get("target_app_id")
        if not isinstance(target_id, str) or not APP_ID_RE.fullmatch(target_id):
            return self._reply(request, "failed", "TARGET_APP_ID_INVALID")
        if type(request.get("confirmed")) is not bool or request["confirmed"] is not True:
            return self._reply(request, "failed", "CONFIRMATION_REQUIRED")
        target = self._targets.get(target_id)
        if target is None:
            return self._reply(request, "failed", "TARGET_APP_NOT_ALLOWLISTED")
        try:
            output_request = OutputRequest.from_payload(request.get("output_request"))
        except ValueError:
            return self._reply(request, "failed", "OUTPUT_REQUEST_INVALID")
        if (not target.minimum_output_count <= output_request.output_count
                <= target.maximum_output_count
                or output_request.profile not in target.output_profiles):
            return self._reply(request, "failed", "OUTPUT_REQUEST_OUTSIDE_TARGET_POLICY")
        if self._backend is None:
            return self._reply(request, "failed", "ISOLATED_BACKEND_UNAVAILABLE")
        if target.requires_output_lease and self._output_lease is None:
            return self._reply(request, "failed", "OUTPUT_LEASE_COORDINATOR_UNAVAILABLE")
        with self._operation_lock:
            with self._lock:
                if self._active is not None:
                    return self._reply(request, "failed", "SESSION_ALREADY_ACTIVE",
                                       self._active.session_id)
                now = self._clock()
                session = _Session(str(uuid.uuid4()), target, created_at=now, last_activity=now)
                self._active = session
            if target.requires_output_lease:
                lease = self._output_lease.acquire(session.session_id, output_request)
                if not isinstance(lease, dict) or lease.get("eligible") is not True:
                    code = (lease.get("error_code") if isinstance(lease, dict)
                            else "OUTPUT_LEASE_UNAVAILABLE")
                    with self._lock:
                        session.state = "failed"
                        session.error_code = code
                        if isinstance(lease, dict) and lease.get("lease_retained") is True:
                            session.output_lease_id = session.session_id
                        else:
                            if self._active is session:
                                self._active = None
                        if session.output_lease_id is not None:
                            session.error_code = "OUTPUT_LEASE_RELEASE_UNVERIFIED"
                    return self._reply(request, "failed", code, session.session_id)
                session.output_lease_id = session.session_id
                session.output_endpoint_keys = tuple(lease["endpoint_keys"])
            # Cancellation can arrive while the output pool is being acquired.
            # Linearize the choice under the runtime lock: if stop won first,
            # release the verified lease and never dispatch a target launch.
            with self._lock:
                cancelled_before_dispatch = session.cancel_requested
            if cancelled_before_dispatch:
                released = self._release_output_lease(session)
                receipt = StopReceipt(True, True, True, True, True,
                                      output_lease_released=released)
                with self._lock:
                    session.receipt = receipt if released else None
                    session.state = "stopped" if released else "failed"
                    session.error_code = (None if released
                                          else "OUTPUT_LEASE_RELEASE_UNVERIFIED")
                    if released:
                        self._receipts[session.session_id] = receipt
                        if self._active is session:
                            self._active = None
                return self._reply(
                    request, "stopped" if released else "failed",
                    "CANCELLED_BEFORE_TARGET_START" if released
                    else "CLEANUP_UNVERIFIED", session.session_id,
                    {"stop_receipt": receipt.__dict__,
                     "output_request": output_request.as_dict(),
                     "output_endpoint_keys": list(session.output_endpoint_keys)})
            try:
                handle = self._backend.start(target, session.session_id,
                                             output_request=output_request,
                                             timeout_seconds=BACKEND_TIMEOUT_SECONDS)
            except BackendStartError as exc:
                cleanup = exc.cleanup_receipt
                safely_absent = ((exc.target_created is False and exc.handle is None)
                                 or isinstance(cleanup, StopReceipt) and cleanup.verified)
                partial_handle = exc.handle if isinstance(exc.handle, TargetHandle) else None
                if partial_handle is not None:
                    target_absent_proven = False
                    with self._lock:
                        session.handle = partial_handle
                        session.state = "failed"
                        session.error_code = "START_CLEANUP_UNVERIFIED"
                    cleanup = self._stop_handle(session, partial_handle)
                    safely_absent = bool(cleanup and cleanup.verified)
                elif safely_absent and session.output_lease_id is not None:
                    target_absent_proven = True
                    safely_absent = self._release_output_lease(session)
                else:
                    target_absent_proven = safely_absent
                with self._lock:
                    session.state = "failed"
                    session.error_code = (exc.error_code if safely_absent
                                          else "OUTPUT_LEASE_RELEASE_UNVERIFIED"
                                          if target_absent_proven and session.output_lease_id is not None
                                          else "START_CLEANUP_UNVERIFIED")
                    if safely_absent and self._active is session:
                        if isinstance(cleanup, StopReceipt):
                            self._receipts[session.session_id] = cleanup
                        self._active = None
                code = (exc.error_code if isinstance(exc.error_code, str)
                        and APP_ID_RE.fullmatch(exc.error_code.lower()) and safely_absent
                        else "START_CLEANUP_UNVERIFIED" if not safely_absent
                        else "TARGET_START_FAILED")
                return self._reply(request, "failed", code, session.session_id)
            except Exception:
                with self._lock:
                    session.state = "failed"
                    session.error_code = "START_CLEANUP_UNVERIFIED"
                return self._reply(request, "failed", "START_CLEANUP_UNVERIFIED",
                                   session.session_id)
            if not isinstance(handle, TargetHandle):
                with self._lock:
                    session.state = "failed"
                    session.error_code = "TARGET_ISOLATION_UNVERIFIED"
                return self._reply(request, "failed", "TARGET_ISOLATION_UNVERIFIED",
                                   session.session_id)
            if not handle.ready_verified:
                with self._lock:
                    session.handle = handle
                cleanup = self._stop_handle(session, handle)
                safely_stopped = bool(cleanup and cleanup.verified)
                with self._lock:
                    if not safely_stopped:
                        session.state = "failed"
                        session.error_code = "CLEANUP_UNVERIFIED"
                return self._reply(request, "failed", "TARGET_ISOLATION_UNVERIFIED"
                                   if safely_stopped else "CLEANUP_UNVERIFIED",
                                   session.session_id)
            with self._lock:
                session.handle = handle
                cancelled = session.cancel_requested
                interrupted = session.state == "interrupted"
                session.state = "stopping" if cancelled else "interrupted" if interrupted else "ready"
                session.last_activity = self._clock()
            if cancelled:
                receipt = self._stop_handle(session, handle)
                return self._reply(request, "stopped" if receipt and receipt.verified else "failed",
                                   "CANCELLED_DURING_START" if receipt and receipt.verified
                                   else "CLEANUP_UNVERIFIED", session.session_id,
                                   {"stop_receipt": receipt.__dict__ if receipt else None})
            capabilities = ["observe"]
            if session.target.action_ids:
                capabilities.append("allowlisted_actions")
            capabilities.extend(("interrupt", "stop"))
            return self._reply(request, "ready", None, session.session_id,
                {"target_app_id": target.app_id, "backend": handle.backend,
                 "output_request": output_request.as_dict(),
                 "output_endpoint_keys": list(session.output_endpoint_keys),
                 "process_id": handle.process_id, "window_id": handle.window_id,
                 "isolation_proof": {
                     "window_absent_from_input_desktop": handle.window_absent_from_input_desktop,
                     "input_desktop_unchanged": handle.input_desktop_unchanged,
                 },
                 "capabilities": capabilities})

    def _ready_session(self, request, *, allow_interrupted=False):
        session, error = self._session(request)
        if error:
            return None, error
        with self._lock:
            if session.cancel_requested or session.state == "stopping":
                return None, self._reply(request, "stopping", "SESSION_STOPPING", session.session_id)
            if session.state == "interrupted" and not allow_interrupted:
                return None, self._reply(request, "interrupted", session.interrupt_reason,
                                         session.session_id)
            if session.state != "ready" and not (allow_interrupted and session.state == "interrupted"):
                return None, self._reply(request, "failed", "SESSION_NOT_READY", session.session_id)
        return session, None

    def _observe(self, request):
        session, error = self._ready_session(request)
        if error:
            return error
        with self._operation_lock:
            session, error = self._ready_session(request)
            if error:
                return error
            if not self._renew_output_lease(session):
                with self._lock:
                    session.state = "stopping"
                receipt = self._stop_handle(session, session.handle)
                return self._reply(request, "stopped" if receipt and receipt.verified else "failed",
                                   "OUTPUT_LEASE_LOST" if receipt and receipt.verified
                                   else "CLEANUP_UNVERIFIED", session.session_id,
                                   {"stop_receipt": receipt.__dict__ if receipt else None})
            try:
                observation = self._backend.observe(session.handle,
                    timeout_seconds=BACKEND_TIMEOUT_SECONDS)
                if (not isinstance(observation, Observation)
                        or not isinstance(observation.accessibility, dict)):
                    raise ValueError("observation shape invalid")
                packet = encode_runtime_packet({
                    # The lifecycle request protocol can evolve independently
                    # from the stable binary observation-envelope protocol.
                    "protocol_version": PACKET_PROTOCOL_VERSION,
                    "request_id": request["request_id"],
                    "session_id": session.session_id,
                    "action": "observe",
                    "target_app_id": session.target.app_id,
                    "backend": session.handle.backend,
                    "process_id": session.handle.process_id,
                    "window_id": session.handle.window_id,
                    "accessibility": observation.accessibility,
                }, observation.png)
            except Exception as exc:
                reason = getattr(exc, "interrupt_reason", None)
                if reason not in INTERRUPT_REASONS:
                    reason = "capture_failed"
                with self._lock:
                    session.state = "interrupted"
                    session.interrupt_reason = reason
                diagnostic_code = getattr(exc, "diagnostic_code", None)
                extra = {}
                if diagnostic_code in PRIVATE_DESKTOP_CAPTURE_DIAGNOSTIC_CODES:
                    extra["diagnostic_code"] = diagnostic_code
                return self._reply(request, "interrupted", reason, session.session_id,
                                   extra or None)
            with self._lock:
                session.last_activity = self._clock()
                cancel = session.cancel_requested
                interrupted = session.state == "interrupted"
                interrupt_reason = session.interrupt_reason
                if cancel:
                    session.state = "stopping"
            if cancel:
                receipt = self._stop_handle(session, session.handle)
                return self._reply(request, "stopped" if receipt and receipt.verified else "failed",
                                   "CANCELLED" if receipt and receipt.verified else "CLEANUP_UNVERIFIED",
                                   session.session_id,
                                   {"stop_receipt": receipt.__dict__ if receipt else None})
            if interrupted:
                return self._reply(request, "interrupted", interrupt_reason, session.session_id)
            return self._reply(request, "running", None, session.session_id,
                               {"packet": packet})

    def _act(self, request):
        session, error = self._ready_session(request)
        if error:
            return error
        action_id = request.get("action_id")
        if (not isinstance(action_id, str) or not ACTION_ID_RE.fullmatch(action_id)
                or action_id not in session.target.action_ids):
            return self._reply(request, "failed", "ACTION_NOT_ALLOWLISTED", session.session_id)
        with self._operation_lock:
            session, error = self._ready_session(request)
            if error:
                return error
            if not self._renew_output_lease(session):
                with self._lock:
                    session.state = "stopping"
                receipt = self._stop_handle(session, session.handle)
                return self._reply(request, "stopped" if receipt and receipt.verified else "failed",
                                   "OUTPUT_LEASE_LOST" if receipt and receipt.verified
                                   else "CLEANUP_UNVERIFIED", session.session_id,
                                   {"stop_receipt": receipt.__dict__ if receipt else None})
            try:
                result = self._backend.act(session.handle, action_id,
                    timeout_seconds=BACKEND_TIMEOUT_SECONDS)
                if not isinstance(result, dict):
                    raise ValueError("action result must be an object")
            except Exception as exc:
                reason = getattr(exc, "interrupt_reason", None)
                if reason not in INTERRUPT_REASONS:
                    reason = "target_lost"
                with self._lock:
                    session.state = "interrupted"
                    session.interrupt_reason = reason
                return self._reply(request, "interrupted", reason, session.session_id)
            with self._lock:
                session.last_activity = self._clock()
                cancelled = session.cancel_requested
                interrupted = session.state == "interrupted"
                if cancelled:
                    session.state = "stopping"
            if cancelled:
                receipt = self._stop_handle(session, session.handle)
                return self._reply(request, "stopped" if receipt and receipt.verified else "failed",
                                   "CANCELLED" if receipt and receipt.verified else "CLEANUP_UNVERIFIED",
                                   session.session_id,
                                   {"stop_receipt": receipt.__dict__ if receipt else None})
            if interrupted:
                return self._reply(request, "interrupted", session.interrupt_reason,
                                   session.session_id)
            return self._reply(request, "running", None, session.session_id,
                               {"action_result": result})

    def _interrupt(self, request):
        reason = request.get("reason")
        if not isinstance(reason, str) or reason not in INTERRUPT_REASONS:
            return self._reply(request, "failed", "INTERRUPT_REASON_INVALID")
        session, error = self._session(request)
        if error:
            return error
        with self._lock:
            if session.state in {"stopped", "failed", "stopping"}:
                return self._reply(request, session.state, "SESSION_NOT_INTERRUPTIBLE",
                                   session.session_id)
            session.state = "interrupted"
            session.interrupt_reason = reason
        return self._reply(request, "interrupted", reason, session.session_id)

    def _heartbeat(self, request):
        session, error = self._session(request)
        if error:
            return error
        with self._operation_lock:
            if not self._renew_output_lease(session):
                with self._lock:
                    session.state = "stopping"
                receipt = self._stop_handle(session, session.handle)
                return self._reply(request, "stopped" if receipt and receipt.verified else "failed",
                                   "OUTPUT_LEASE_LOST" if receipt and receipt.verified
                                   else "CLEANUP_UNVERIFIED", session.session_id,
                                   {"stop_receipt": receipt.__dict__ if receipt else None})
            with self._lock:
                if session.cancel_requested:
                    return self._reply(request, "stopping", "SESSION_STOPPING", session.session_id)
                now = self._clock()
                if now - session.last_activity > IDLE_TIMEOUT_SECONDS:
                    session.cancel_requested = True
                    session.state = "stopping"
                    expired = True
                else:
                    session.last_activity = now
                    expired = False
                    state = session.state
            if expired:
                receipt = self._stop_handle(session, session.handle)
                return self._reply(request, "stopped" if receipt and receipt.verified else "failed",
                                   "IDLE_TIMEOUT" if receipt and receipt.verified else "CLEANUP_UNVERIFIED",
                                   session.session_id,
                                   {"stop_receipt": receipt.__dict__ if receipt else None})
            return self._reply(request, state, None, session.session_id)

    def _cancel(self, request):
        session_id = request.get("session_id")
        if not isinstance(session_id, str) or not UUID_RE.fullmatch(session_id):
            return self._reply(request, "failed", "SESSION_ID_INVALID")
        with self._lock:
            session = self._active if self._active and self._active.session_id == session_id else None
            receipt = self._receipts.get(session_id)
            if session:
                cleanup_unknown = (session.handle is None
                                   and session.error_code == "START_CLEANUP_UNVERIFIED")
                if cleanup_unknown:
                    return self._reply(request, "failed", "CLEANUP_UNVERIFIED", session_id)
                session.cancel_requested = True
                session.state = "stopping"
        if receipt is not None:
            return self._reply(request, "stopped", None, session_id,
                               {"stop_receipt": receipt.__dict__})
        if session is None:
            return self._reply(request, "failed", "SESSION_NOT_FOUND", session_id)
        with self._operation_lock:
            with self._lock:
                receipt = self._receipts.get(session_id)
                if receipt is not None:
                    return self._reply(request, "stopped", None, session_id,
                                       {"stop_receipt": receipt.__dict__})
                handle = session.handle
                if handle is None:
                    if (session.state == "failed"
                            and session.error_code == "START_CLEANUP_UNVERIFIED"):
                        return self._reply(request, "failed", "CLEANUP_UNVERIFIED", session_id)
                    if session.output_lease_id is not None:
                        released = self._release_output_lease(session)
                        receipt = StopReceipt(True, True, True, True, True,
                                              output_lease_released=released)
                        if released:
                            session.state = "stopped"
                            session.error_code = None
                            self._receipts[session_id] = receipt
                            if self._active is session:
                                self._active = None
                        return self._reply(request, "stopped" if released else "failed",
                                           None if released else "CLEANUP_UNVERIFIED",
                                           session_id, {"stop_receipt": receipt.__dict__})
                    if session.state == "failed":
                        return self._reply(request, "failed", "CLEANUP_UNVERIFIED", session_id)
                    # start() owns the operation lock and will observe the cancel flag
                    # immediately after it has a verified handle.
                    return self._reply(request, "stopping", "CANCEL_REQUESTED", session_id)
            receipt = self._stop_handle(session, handle)
        return self._reply(request, "stopped" if receipt and receipt.verified else "failed",
                           None if receipt and receipt.verified else "CLEANUP_UNVERIFIED",
                           session_id, {"stop_receipt": receipt.__dict__ if receipt else None})

    def _stop_handle(self, session, handle):
        if handle is None:
            return None
        try:
            receipt = self._backend.stop(handle, timeout_seconds=BACKEND_TIMEOUT_SECONDS)
        except Exception:
            with self._lock:
                session.state = "failed"
                session.error_code = "CLEANUP_UNVERIFIED"
            return None
        if not isinstance(receipt, StopReceipt):
            verified = False
        else:
            verified = receipt.verified
        output_lease_released = (self._release_output_lease(session)
                                 if verified else session.output_lease_id is None)
        if isinstance(receipt, StopReceipt) and session.output_lease_id is not None:
            receipt = StopReceipt(
                receipt.stopped, receipt.target_stopped, receipt.job_closed,
                receipt.desktop_removed, receipt.input_desktop_unchanged,
                output_lease_released=output_lease_released,
                profile_data_removed=receipt.profile_data_removed)
        verified = verified and output_lease_released
        with self._lock:
            session.receipt = receipt if isinstance(receipt, StopReceipt) else None
            session.state = "stopped" if verified else "failed"
            session.error_code = None if verified else "CLEANUP_UNVERIFIED"
            if verified:
                self._receipts[session.session_id] = receipt
                if self._active is session:
                    self._active = None
        return receipt if isinstance(receipt, StopReceipt) else None

    def _release_output_lease(self, session) -> bool:
        lease_id = session.output_lease_id
        if lease_id is None:
            return True
        coordinator = self._output_lease
        if coordinator is None:
            return False
        try:
            released = coordinator.release(lease_id) is True
        except Exception:
            released = False
        if released:
            session.output_lease_id = None
        return released

    def _renew_output_lease(self, session) -> bool:
        if session.output_lease_id is None:
            return True
        coordinator = self._output_lease
        if coordinator is None:
            return False
        try:
            return coordinator.heartbeat(session.output_lease_id) is True
        except Exception:
            return False

    def dispose(self):
        with self._lock:
            self._owners_disposing = True
            owner_children = list(self._owner_runtimes.values())
        for child in owner_children:
            child.dispose()
        with self._lock:
            active = self._active
        if active is not None:
            request = {"protocol_version": PROTOCOL_VERSION,
                       "request_id": str(uuid.uuid4()), "action": "stop",
                       "session_id": active.session_id}
            result = self.handle_request(request)
            if result.get("state") != "stopped":
                with self._lock:
                    self._initialized = True
                raise RuntimeError("Task runtime cleanup not verified; module kept initialized")
        self._watchdog_stop.set()
        watchdog = self._watchdog_thread
        if watchdog is not None and watchdog is not threading.current_thread():
            watchdog.join(timeout=BACKEND_TIMEOUT_SECONDS + self._watchdog_interval + 1.0)
            if watchdog.is_alive():
                raise RuntimeError("Task session watchdog did not stop")
        with self._lock:
            self._watchdog_thread = None
            self._initialized = False

    def _handle_owner_request(self, request):
        # Routing prevents accidental cross-chat operations; owner IDs are not
        # an authentication boundary between processes of the same OS user.
        owner = request.get("owner_id")
        if (not isinstance(owner, str) or not 1 <= len(owner) <= 128
                or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for c in owner)):
            return self._reply(request, "failed", "OWNER_ID_INVALID")
        inner = dict(request)
        inner.pop("owner_id")
        action = inner.get("action")
        if (type(inner.get("protocol_version")) is not int
                or inner.get("protocol_version") != PROTOCOL_VERSION
                or not isinstance(action, str) or action not in self._FIELDS
                or set(inner) != self._FIELDS[action]
                or not isinstance(inner.get("request_id"), str)
                or not UUID_RE.fullmatch(inner["request_id"])):
            return self._reply(request, "failed", "REQUEST_INVALID")
        if action == "start":
            target_id = inner.get("target_app_id")
            target = self._targets.get(target_id) if isinstance(target_id, str) else None
            if (target is None or target.launch is None
                    or target.isolation_mode != "private_desktop"
                    or not any("{VDM_TASK_PROFILE}" in arg for arg in target.launch.arguments)):
                return self._reply(request, "failed", "ISOLATED_APP_PROFILE_REQUIRED")
        with self._lock:
            if not self._initialized:
                return self._reply(request, "failed", "RUNTIME_NOT_INITIALIZED")
            if self._owners_disposing and action == "start":
                return self._reply(request, "failed", "RUNTIME_STOPPING")
            child = self._owner_runtimes.get(owner)
            if child is None:
                if action not in {"start", "status"}:
                    return self._reply(request, "failed", "OWNER_SESSION_NOT_FOUND")
                if len(set(self._owner_runtimes) | set(self._owner_saved_states) | {owner}) > 8:
                    return self._reply(request, "failed", "OWNER_CAPACITY_REACHED")
                child = TaskSessionRuntime(
                    backend=self._backend, target_registry=self._targets,
                    output_lease=self._output_lease, clock=self._clock,
                    watchdog_interval=self._watchdog_interval)
                child.restore(self._owner_saved_states.get(owner))
                child.init()
                self._owner_runtimes[owner] = child
        result = child.handle_request(inner)
        return dict(result, owner_id=owner)


def create():
    return TaskSessionRuntime()

