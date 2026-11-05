"""Only Chat lease for manager-created virtual outputs; no target-app execution."""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from modules.only_chat_debug_display.module import NamedPipeServer
from modules.only_chat_task_runtime.module import (
    APP_PATH_REGISTRY, process_image_matches, registered_only_chat_exe_path,
)
from modules.task_display_lease.authority import (
    TaskDisplayLeaseAuthority, API_VERSION as LEASE_PROTOCOL_VERSION,
)
from modules.task_display_lease.replay_store import ReplayNonceStore

API_VERSION = 1
MODULE_ID = "only_chat_output_lease"
VERSION = "0.1.0"
PIPE_PATH = r"\\.\pipe\OnlyChatAiVirtualOutputLease.v2"
MAX_REQUEST_BYTES = 8192
MAX_REPLY_BYTES = 8192
CONSUMER_ID = "only-chat-windows"
INSTANCE_ID = "34e94ec8b4ef4c71a0dcf2b3ac94e744"
ALLOWED_SCOPE = frozenset({"display-output"})


def _replay_store_path():
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("LOCALAPPDATA_UNAVAILABLE")
    return Path(local_app_data) / "AI Virtual Display Manager" / "only-chat-output-lease-replay.json"


class OnlyChatOutputLeaseModule:
    """Authenticates Only Chat and leases only exact active manager-created outputs."""

    def __init__(self, *, pool=None, topology_reader=None, server_factory=None,
                 registry_path=APP_PATH_REGISTRY, process_image_checker=process_image_matches,
                 monotonic=None, utc_now=None, id_factory=None,
                 replay_store_factory=None):
        self._pool = pool
        self._topology_reader = topology_reader
        self._server_factory = server_factory or NamedPipeServer
        self._registry_path = Path(registry_path)
        self._process_image_checker = process_image_checker
        self._monotonic = monotonic
        self._utc_now = utc_now
        self._id_factory = id_factory
        self._replay_store_factory = replay_store_factory or (
            lambda: ReplayNonceStore(_replay_store_path()))
        self._replay_store = None
        self._server = None
        self._authority = None
        self._lock = threading.RLock()
        self._startup_error = None
        self._initialized = False

    def _expected_client_path(self):
        return registered_only_chat_exe_path(self._registry_path)

    def _peer_authorized(self, pid):
        path = self._expected_client_path()
        if path is None:
            return False
        try:
            return self._process_image_checker(pid, path) is True
        except Exception:
            return False

    def _pool_keys(self, topology):
        if self._pool is None or not isinstance(topology, dict):
            return set()
        from modules.virtual_output_pool.module import mode_identity_key
        modes = {}
        for row in topology.get("displays", []):
            if not isinstance(row, dict):
                return set()
            try:
                key = mode_identity_key(row.get("display_name"), row.get("monitor_device_id"),
                                        row.get("monitor_path"))
                modes[key] = (int(row.get("width")), int(row.get("height")),
                              int(row.get("refresh_rate")))
            except (TypeError, ValueError):
                return set()
        result = self._pool.task_leaseable_endpoint_keys(modes)
        return set(result.get("endpoint_keys", [])) if result.get("eligible") is True else set()

    def _heartbeat(self, lease_id):
        if self._pool is None:
            raise RuntimeError("OUTPUT_POOL_UNAVAILABLE")
        self._pool.heartbeat(lease_id, confirmed=True)

    def _release(self, lease_id):
        if self._pool is not None:
            self._pool.release(lease_id)

    def bind_dependencies(self, *, pool, topology_reader):
        if pool is None or not callable(topology_reader):
            raise TypeError("A configured virtual-output pool and topology reader are required")
        with self._lock:
            if self._authority and self._authority._leases:
                raise RuntimeError("Cannot rebind dependencies while Only Chat output leases are active")
            self._pool = pool
            self._topology_reader = topology_reader
            # ModuleHost calls init() before the composition root binds live readers.
            if self._authority is not None:
                self._authority.topology_reader = topology_reader

    def init(self):
        if self._initialized:
            raise RuntimeError("Only Chat output lease provider already initialized")
        self._initialized = True
        try:
            self._replay_store = self._replay_store_factory()
        except Exception as exc:
            self._startup_error = f"REPLAY_GUARD_UNAVAILABLE:{type(exc).__name__}"
            return
        options = {
            "consumer_id": CONSUMER_ID,
            "instance_id": INSTANCE_ID,
            "peer_authorized": self._peer_authorized,
            "pool_eligible_endpoints": self._pool_keys,
            "topology_reader": self._topology_reader or (lambda: {}),
            "heartbeat": self._heartbeat,
            "release_pool_lease": self._release,
            "consume_replay": self._replay_store.consume,
            "allowed_scope": ALLOWED_SCOPE,
        }
        if self._monotonic is not None:
            options["monotonic"] = self._monotonic
        if self._utc_now is not None:
            options["utc_now"] = self._utc_now
        if self._id_factory is not None:
            options["id_factory"] = self._id_factory
        self._authority = TaskDisplayLeaseAuthority(**options)
        if "--self-check" in sys.argv:
            self._startup_error = "BROKER_NOT_STARTED_IN_SELF_CHECK"
            return

    def start_listener(self):
        """Bind only from the production composition root after dependencies are attached."""
        if not self._initialized:
            raise RuntimeError("Only Chat output lease provider is not initialized")
        if self._authority is None:
            self._startup_error = self._startup_error or "PROVIDER_NOT_READY"
            return False
        if "--self-check" in sys.argv:
            self._startup_error = "BROKER_NOT_STARTED_IN_SELF_CHECK"
            return False
        if self._pool is None or not callable(self._topology_reader):
            self._startup_error = "DEPENDENCIES_NOT_BOUND"
            return False
        if self._server is not None:
            raise RuntimeError("Only Chat output lease listener already started")
        try:
            self._server = self._server_factory(
                lambda _request: self._failure(None, "CLIENT_AUTH_REQUIRED"),
                pipe_path=PIPE_PATH, max_request_bytes=MAX_REQUEST_BYTES,
                max_reply_bytes=MAX_REPLY_BYTES,
                client_authenticator=self._peer_authorized,
                rejected_handler=lambda request: self._failure(
                    request, "CONSUMER_PROCESS_UNAUTHORIZED"),
                authenticated_handler=self._handle_authenticated)
            self._server.start()
            self._startup_error = None
            return True
        except Exception as exc:
            self._startup_error = f"BROKER_START_FAILED:{type(exc).__name__}"
            try:
                if self._server:
                    self._server.stop()
            except Exception:
                pass
            self._server = None
            return False

    def _handle_authenticated(self, request, peer_pid):
        authority = self._authority
        if authority is None:
            return self._failure(request, "PROVIDER_NOT_READY")
        result = authority.handle(request, peer_pid)
        if result.get("state") in {"acquired", "valid"}:
            result = dict(result)
            result["display_scope"] = result.pop("capture_scope", [])
        return result

    @staticmethod
    def _failure(request, code):
        return {"version": LEASE_PROTOCOL_VERSION,
                "request_id": request.get("request_id") if isinstance(request, dict) else None,
                "nonce": request.get("nonce") if isinstance(request, dict) else None,
                "state": "denied", "error_code": code}

    def refresh(self):
        server = self._server
        pipe_ready = bool(server and server.ready)
        identity_ready = self._expected_client_path() is not None
        dependencies_ready = self._pool is not None and callable(self._topology_reader)
        provider_ready = (pipe_ready and identity_ready and bool(self._authority)
                          and dependencies_ready)
        error_code = self._startup_error
        if error_code is None and not identity_ready:
            error_code = "ONLY_CHAT_IDENTITY_UNVERIFIED"
        elif error_code is None and pipe_ready and not dependencies_ready:
            error_code = "DEPENDENCIES_NOT_BOUND"
        elif error_code is None and not pipe_ready:
            error_code = "BROKER_UNAVAILABLE"
        return {"pipe_path": PIPE_PATH, "pipe_ready": pipe_ready,
                "client_identity_ready": identity_ready,
                "client_identity_policy": "registry_exact_image_path",
                "provider_available": provider_ready,
                "protocol_version": LEASE_PROTOCOL_VERSION,
                "active_lease_count": len(self._authority._leases) if self._authority else 0,
                "allowed_scope": sorted(ALLOWED_SCOPE),
                "error_code": None if provider_ready else error_code or "PROVIDER_NOT_READY",
                "lease_acquisition": ("requires_active_manager_created_unmirrored_output"
                                      if provider_ready else "fail_closed")}

    def reap_expired(self):
        return self._authority.reap_expired() if self._authority else 0

    def action(self, name, _payload):
        if name != "lease_status":
            raise ValueError("Only Chat output lease module exports status only")
        return self.refresh()

    def snapshot(self):
        if self._server and self._server.active_clients:
            raise RuntimeError("An Only Chat output lease request is active; reload is deferred")
        if self._server and self._server.ready:
            raise RuntimeError("The process-global Only Chat output lease pipe requires manager restart to reload")
        return {"inactive": True}

    def restore(self, state):
        if state not in (None, {"inactive": True}):
            raise ValueError("Only Chat output lease provider state cannot be restored")

    def reload_blocked(self):
        return bool(self._server and self._server.active_clients)

    def dispose(self):
        with self._lock:
            if self._authority and self._authority._leases:
                raise RuntimeError("Active Only Chat output leases prevent provider shutdown")
            if self._server:
                self._server.stop()
            self._server = None
            self._authority = None
            self._initialized = False


ACTIONS = ("lease_status",)


def create():
    return OnlyChatOutputLeaseModule()
