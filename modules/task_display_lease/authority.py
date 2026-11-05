"""Pure lease authority core; platform identity and live display proof are injected."""
from __future__ import annotations

import base64
import hashlib
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from threading import RLock

API_VERSION = 2
MODULE_ID = "task_display_lease"
VERSION = "0.1.1"
MAX_TTL_SECONDS = 120
MAX_ACTIVE_LEASES = 8
MAX_REPLAY_ENTRIES = 4096
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89aAbB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
ALLOWED_SCOPE = frozenset({"launch-client", "client-input", "framebuffer-capture"})
KNOWN_SCOPES = ALLOWED_SCOPE | frozenset({"display-output"})


class LeaseError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _uuid(value):
    return isinstance(value, str) and UUID_RE.fullmatch(value) is not None


def _nonce(value):
    if not isinstance(value, str) or NONCE_RE.fullmatch(value) is None:
        return False
    try:
        return len(base64.urlsafe_b64decode(value + "=")) == 32
    except (ValueError, base64.binascii.Error):
        return False


class TaskDisplayLeaseAuthority:
    """Issue short leases only when caller identity, pool and topology all agree.

    All callbacks must be read-only except heartbeat/release, which touch the
    already user-confirmed output-pool lease ledger and never change topology.
    """

    def __init__(self, *, consumer_id, instance_id, peer_authorized,
                 pool_eligible_endpoints, topology_reader, heartbeat=None,
                 release_pool_lease=None, monotonic=time.monotonic,
                 utc_now=None, id_factory=None, consume_replay=None,
                 allowed_scope=ALLOWED_SCOPE):
        self.consumer_id = consumer_id
        self.instance_id = instance_id
        self.peer_authorized = peer_authorized
        self.pool_eligible_endpoints = pool_eligible_endpoints
        self.topology_reader = topology_reader
        self.heartbeat = heartbeat or (lambda _lease: None)
        self.release_pool_lease = release_pool_lease or (lambda _lease: None)
        self.clock = monotonic
        self.utc_now = utc_now or (lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self.id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self.consume_replay = consume_replay
        if (not isinstance(allowed_scope, (set, frozenset, list, tuple))
                or not allowed_scope
                or any(not isinstance(scope, str) or scope not in KNOWN_SCOPES
                       for scope in allowed_scope)):
            raise ValueError("Lease scopes must be a non-empty subset of the known scope set")
        self.allowed_scope = frozenset(allowed_scope)
        self._leases = {}
        self._seen = set()
        self._released = {}
        self._lock = RLock()

    def handle(self, request, peer_pid):
        if not isinstance(request, dict):
            return self._denied({}, "REQUEST_INVALID")
        try:
            with self._lock:
                return self._handle_locked(request, peer_pid)
        except LeaseError as exc:
            return self._denied(request, exc.code)
        except Exception:
            return self._denied(request, "PROVIDER_STATE_UNAVAILABLE")

    def reap_expired(self):
        """Release expired pool leases without touching display topology."""
        with self._lock:
            expired = [lease for lease in self._leases.values()
                       if self.clock() >= lease["expires"]]
            for lease in expired:
                self._expire(lease)
            return len(expired)

    def _handle_locked(self, request, peer_pid):
        action = request.get("action")
        required = ({"version", "request_id", "nonce", "action", "consumer_id",
                     "run_id", "workbench_pid", "client_session_id", "instance_id",
                     "requested_scope", "requested_ttl_seconds"} if action == "acquire" else
                    {"version", "request_id", "nonce", "action", "consumer_id",
                     "run_id", "workbench_pid", "client_session_id", "instance_id", "lease_id"})
        if action not in {"acquire", "validate", "renew", "release"} or set(request) != required:
            raise LeaseError("REQUEST_INVALID")
        if type(request.get("version")) is not int or request["version"] != API_VERSION:
            raise LeaseError("PROTOCOL_VERSION_UNSUPPORTED")
        rid, nonce = request.get("request_id"), request.get("nonce")
        if not _uuid(rid) or not _nonce(nonce):
            raise LeaseError("REQUEST_ID_OR_NONCE_INVALID")
        replay_key = (rid, nonce)
        if self.consume_replay is not None:
            if self.consume_replay(*replay_key) is not True:
                raise LeaseError("REQUEST_REPLAYED")
        else:
            if replay_key in self._seen:
                raise LeaseError("REQUEST_REPLAYED")
            if len(self._seen) >= MAX_REPLAY_ENTRIES:
                raise LeaseError("REPLAY_GUARD_CAPACITY_REACHED")
            self._seen.add(replay_key)
        self._verify_owner(request, peer_pid)
        if action == "acquire":
            return self._acquire(request, peer_pid)
        return self._operate(request, peer_pid, action)

    def _verify_owner(self, request, peer_pid):
        if (type(peer_pid) is not int or peer_pid <= 0
                or request.get("workbench_pid") != peer_pid
                or request.get("consumer_id") != self.consumer_id
                or request.get("instance_id") != self.instance_id
                or not _uuid(request.get("run_id"))
                or not _uuid(request.get("client_session_id"))):
            raise LeaseError("OWNER_OR_SESSION_MISMATCH")
        if self.peer_authorized(peer_pid) is not True:
            raise LeaseError("CONSUMER_PROCESS_UNAUTHORIZED")

    def _current_endpoint(self):
        topology = self.topology_reader()
        if not isinstance(topology, dict) or topology.get("topology_verified") is not True:
            raise LeaseError("TOPOLOGY_UNAVAILABLE")
        displays = topology.get("displays")
        if not isinstance(displays, list):
            raise LeaseError("TOPOLOGY_UNAVAILABLE")
        eligible = self.pool_eligible_endpoints(topology)
        if not isinstance(eligible, (set, frozenset, list, tuple)) or not eligible:
            raise LeaseError("NO_ACTIVE_UNMIRRORED_OUTPUT")
        generation = topology.get("topology_generation")
        if not isinstance(generation, str) or not generation:
            stable_paths = sorted((str(item.get("display_name", "")),
                                   str(item.get("monitor_path", "")),
                                   str(item.get("cloned", "")))
                                  for item in displays if isinstance(item, dict))
            generation = hashlib.sha256(repr(stable_paths).encode("utf-8")).hexdigest()
        matches = []
        for row in displays:
            if not isinstance(row, dict):
                continue
            key = "\n".join(str(row.get(field, "")).strip() for field in
                             ("display_name", "monitor_device_id", "monitor_path"))
            if (key in eligible and row.get("attached") is True
                    and row.get("virtual") is True
                    and row.get("monitor_identity_verified") is True
                    and row.get("topology_verified") is True
                    and row.get("verified") is True
                    and row.get("cloned") is False
                    and row.get("clone_source_count") == 1
                    and str(row.get("monitor_parent", "")).strip()
                    and str(topology.get("mtt_device_instance_id", "")).strip().casefold()
                    == str(row.get("monitor_parent", "")).strip().casefold()
                    and str(row.get("monitor_path", "")).strip()):
                matches.append(row)
        if len(matches) != 1:
            raise LeaseError("NO_ACTIVE_UNMIRRORED_OUTPUT" if not matches
                             else "OUTPUT_IDENTITY_AMBIGUOUS")
        row = matches[0]
        return {"device_path": row["monitor_path"],
                "topology_generation": generation,
                "display_name": row["display_name"]}

    def _acquire(self, request, peer_pid):
        scope = request.get("requested_scope")
        ttl = request.get("requested_ttl_seconds")
        if (not isinstance(scope, list) or not scope
                or any(not isinstance(item, str) or item not in self.allowed_scope
                       for item in scope)
                or len(scope) != len(set(scope))):
            raise LeaseError("SCOPE_INVALID")
        if type(ttl) is not int or not 1 <= ttl <= MAX_TTL_SECONDS:
            raise LeaseError("TTL_INVALID")
        if len(self._leases) >= MAX_ACTIVE_LEASES:
            raise LeaseError("LEASE_CAPACITY_REACHED")
        endpoint = self._current_endpoint()
        lease_id = self.id_factory()
        if not _uuid(lease_id):
            raise LeaseError("PROVIDER_ID_INVALID")
        lease = {"lease_id": lease_id, "run_id": request["run_id"],
                 "workbench_pid": peer_pid, "client_session_id": request["client_session_id"],
                 "instance_id": self.instance_id, "scope": list(scope),
                 "endpoint": endpoint, "issued": self.clock(), "expires": self.clock() + ttl,
                 "ttl": ttl, "issued_at_utc": self.utc_now(),
                 "expires_at_utc": self._utc_after(ttl), "revocation_generation": 1}
        self.heartbeat(lease_id)
        self._leases[lease_id] = lease
        return {"version": API_VERSION, "request_id": request["request_id"],
                "nonce": request["nonce"], "state": "acquired", **self._public(lease)}

    def _operate(self, request, peer_pid, action):
        lease = self._leases.get(request.get("lease_id"))
        if lease is None:
            owner = self._released.get(request.get("lease_id"))
            expected = (request.get("run_id"), peer_pid,
                        request.get("client_session_id"), request.get("instance_id"))
            if action == "release" and owner and owner[0] == expected:
                return {"version": API_VERSION, "request_id": request["request_id"],
                        "nonce": request["nonce"], "state": "released",
                        "lease_id": request["lease_id"], "run_id": request["run_id"]}
            raise LeaseError("LEASE_NOT_FOUND")
        for key in ("run_id", "client_session_id", "instance_id"):
            if request.get(key) != lease[key]:
                raise LeaseError("LEASE_OWNER_MISMATCH")
        if peer_pid != lease["workbench_pid"]:
            raise LeaseError("LEASE_OWNER_MISMATCH")
        if self.clock() >= lease["expires"]:
            self._expire(lease)
            raise LeaseError("LEASE_EXPIRED")
        try:
            endpoint = self._current_endpoint()
        except LeaseError as exc:
            lease["revocation_generation"] += 1
            self._expire(lease)
            raise LeaseError("TOPOLOGY_CHANGED") from exc
        if endpoint != lease["endpoint"]:
            lease["revocation_generation"] += 1
            self._expire(lease)
            raise LeaseError("TOPOLOGY_CHANGED")
        if action == "release":
            self._expire(lease)
            return {"version": API_VERSION, "request_id": request["request_id"],
                    "nonce": request["nonce"], "state": "released",
                    "lease_id": lease["lease_id"], "run_id": lease["run_id"]}
        if action == "renew":
            lease["issued"] = self.clock()
            lease["expires"] = self.clock() + lease["ttl"]
            lease["issued_at_utc"] = self.utc_now()
            lease["expires_at_utc"] = self._utc_after(lease["ttl"])
            self.heartbeat(lease["lease_id"])
        return {"version": API_VERSION, "request_id": request["request_id"],
                "nonce": request["nonce"], "state": "valid", **self._public(lease)}

    def _expire(self, lease):
        self._leases.pop(lease["lease_id"], None)
        if len(self._released) >= MAX_ACTIVE_LEASES * 4:
            oldest = min(self._released, key=lambda key: self._released[key][1])
            self._released.pop(oldest, None)
        owner = (lease["run_id"], lease["workbench_pid"],
                 lease["client_session_id"], lease["instance_id"])
        self._released[lease["lease_id"]] = (owner, self.clock())
        self.release_pool_lease(lease["lease_id"])

    def _public(self, lease):
        return {"lease_id": lease["lease_id"], "run_id": lease["run_id"],
                "workbench_pid": lease["workbench_pid"],
                "client_session_id": lease["client_session_id"],
                "instance_id": lease["instance_id"],
                "display": {"device_path": lease["endpoint"]["device_path"],
                            "active": True, "mirrored": False,
                            "visible_on_physical_output": False,
                            "topology_generation": lease["endpoint"]["topology_generation"]},
                "capture_scope": list(lease["scope"]),
                "issued_at_utc": lease["issued_at_utc"],
                "expires_at_utc": lease["expires_at_utc"],
                "revocation_generation": lease["revocation_generation"]}

    def _utc_after(self, seconds):
        value = self.utc_now()
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return (moment + timedelta(seconds=seconds)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except (AttributeError, TypeError, ValueError):
            raise LeaseError("UTC_CLOCK_UNAVAILABLE") from None

    @staticmethod
    def _denied(request, code):
        return {"version": API_VERSION,
                "request_id": request.get("request_id") if isinstance(request, dict) else None,
                "nonce": request.get("nonce") if isinstance(request, dict) else None,
                "state": "denied", "error_code": code}
