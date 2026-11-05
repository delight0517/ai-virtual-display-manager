"""Bounded, content-free diagnostics for display/window recovery incidents."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

API_VERSION = 1
MODULE_ID = "display_diagnostics"
VERSION = "0.1.1"
ACTIONS = ("record", "read", "clear")
MAX_BYTES = 1_048_576
MAX_AGE_DAYS = 30
MAX_EVENTS = 5000
EVENTS = frozenset({
    "window_observed", "restore_requested", "restore_result", "guard_state",
    "guard_start_result", "topology_state", "manager_state", "display_settings_change",
})
APP_KINDS = frozenset({"discord", "browser", "explorer", "focusfense", "other"})
WINDOW_STATES = frozenset({"normal", "minimized", "maximized", "hidden", "unknown"})
OUTPUT_KINDS = frozenset({"primary", "physical_secondary", "virtual", "offscreen", "unknown"})
RESULTS = frozenset({"started", "succeeded", "failed", "unavailable", "skipped", "closed", "unknown"})
SETTING_KEYS = frozenset({"physical_window_guard_enabled", "display_topology_changed"})
TOPOLOGY_KINDS = frozenset({"physical_and_virtual", "physical_only", "virtual_only", "unknown"})


def _timestamp(value):
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, str):
        value = _parse_timestamp(value)
    if not isinstance(value, datetime):
        raise ValueError("timestamp must be a datetime")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="milliseconds")


def _parse_timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)", value):
        raise ValueError("timestamp must be a UTC ISO-8601 value")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class DisplayDiagnosticStore:
    """Stores allowlisted metadata only, with atomic bounded retention."""

    def __init__(self, path=None, *, clock=None):
        if path is None:
            root = os.environ.get("LOCALAPPDATA")
            if not root:
                raise RuntimeError("LOCALAPPDATA is unavailable")
            path = Path(root) / "AI Virtual Display Manager" / "diagnostics" / "display-events.jsonl"
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._retention_clock = lambda: datetime.now(timezone.utc)
        self._lock = threading.RLock()

    @staticmethod
    def _clean(event):
        if not isinstance(event, dict) or event.get("event") not in EVENTS:
            raise ValueError("unsupported diagnostic event")
        out = {"event": event["event"], "timestamp": _timestamp(event.get("timestamp"))}
        app = event.get("app")
        if app is not None:
            if app not in APP_KINDS:
                raise ValueError("unsupported app category")
            out["app"] = app
        state = event.get("window_state")
        if state is not None:
            if state not in WINDOW_STATES:
                raise ValueError("unsupported window state")
            out["window_state"] = state
        output = event.get("output")
        if output is not None:
            if output not in OUTPUT_KINDS:
                raise ValueError("unsupported output category")
            out["output"] = output
        result = event.get("result")
        if result is not None:
            if result not in RESULTS:
                raise ValueError("unsupported operation result")
            out["result"] = result
        topology = event.get("topology")
        if topology is not None:
            if topology not in TOPOLOGY_KINDS:
                raise ValueError("unsupported topology category")
            out["topology"] = topology
        output_count = event.get("output_count")
        if output_count is not None:
            if type(output_count) is not int or not 0 <= output_count <= 16:
                raise ValueError("output_count must be between zero and sixteen")
            out["output_count"] = output_count
        setting = event.get("setting")
        if setting is not None:
            if setting not in SETTING_KEYS:
                raise ValueError("unsupported setting key")
            out["setting"] = setting
        if "enabled" in event:
            if type(event["enabled"]) is not bool:
                raise ValueError("enabled must be a boolean")
            out["enabled"] = event["enabled"]
        bounds = event.get("bounds")
        if bounds is not None:
            if (not isinstance(bounds, (list, tuple)) or len(bounds) != 4
                    or any(type(item) is not int for item in bounds)):
                raise ValueError("bounds must contain four integers")
            left, top, right, bottom = bounds
            if right <= left or bottom <= top:
                raise ValueError("bounds must have positive area")
            out["bounds"] = [left, top, right, bottom]
        reason = event.get("reason")
        if reason is not None:
            if reason not in {"topology_unverified", "window_minimized", "identity_changed",
                              "api_failed", "permission_denied", "timeout", "no_window",
                              "no_physical_output", "unknown", "monitor_identity_refresh_pending",
                              "monitor_identity_refresh_failed", "topology_provider_exception",
                              "display_path_query_failed", "monitor_identity_unverified"}:
                raise ValueError("unsupported diagnostic reason")
            out["reason"] = reason
        refresh_state = event.get("monitor_identity_refresh_state")
        if refresh_state is not None:
            if refresh_state not in {"fresh", "refreshing", "pending", "error", "unavailable", "timeout"}:
                raise ValueError("unsupported monitor identity refresh state")
            out["monitor_identity_refresh_state"] = refresh_state
        for key in ("monitor_identity_age_ms", "display_count", "identified_display_count"):
            value = event.get(key)
            if value is not None:
                limit = 86_400_000 if key == "monitor_identity_age_ms" else 16
                if type(value) is not int or not 0 <= value <= limit:
                    raise ValueError(f"{key} is outside its allowed range")
                out[key] = value
        cache_hit = event.get("monitor_identity_cache_hit")
        if cache_hit is not None:
            if type(cache_hit) is not bool:
                raise ValueError("monitor_identity_cache_hit must be a boolean")
            out["monitor_identity_cache_hit"] = cache_hit
        error_type = event.get("provider_error_type")
        if error_type is not None:
            if error_type not in {"OSError", "TimeoutError", "RuntimeError", "ValueError", "Other"}:
                raise ValueError("unsupported provider error type")
            out["provider_error_type"] = error_type
        return out

    def _load(self):
        if not self.path.exists():
            return []
        rows = []
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        value = json.loads(line)
                        clean = self._clean(value)
                        rows.append(clean)
                    except (ValueError, TypeError, json.JSONDecodeError):
                        continue
        except (OSError, UnicodeError):
            return []
        now = self._retention_clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        cutoff = now.astimezone(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
        retained = []
        for row in rows[-MAX_EVENTS:]:
            try:
                if _parse_timestamp(row["timestamp"]) >= cutoff:
                    retained.append(row)
            except ValueError:
                continue
        return retained

    def _write(self, rows):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = "".join(json.dumps(row, separators=(",", ":"), ensure_ascii=True) + "\n"
                         for row in rows[-MAX_EVENTS:]).encode("utf-8")
        if len(encoded) > MAX_BYTES:
            raise ValueError("diagnostic retention exceeded its size limit")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=self.path.parent,
                                             prefix=".display-events-", suffix=".tmp",
                                             delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except Exception:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise

    def append(self, event):
        clean = self._clean(event)
        with self._lock:
            rows = self._load()
            rows.append(clean)
            self._write(rows)
            return {"stored": True, "event_count": len(rows), "retention_days": MAX_AGE_DAYS,
                    "max_bytes": MAX_BYTES}

    def read(self, limit=500):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._lock:
            rows = self._load()
            self._write(rows)
            return {"events": rows[-limit:], "event_count": len(rows),
                    "retention_days": MAX_AGE_DAYS, "max_bytes": MAX_BYTES,
                    "content_recorded": False}

    def clear(self):
        with self._lock:
            self._write([])
            return {"cleared": True, "event_count": 0}


class DisplayDiagnosticsModule:
    def __init__(self, store_factory=None):
        self.store_factory = store_factory or DisplayDiagnosticStore
        self.store = None

    def init(self):
        self.store = self.store_factory()

    def dispose(self):
        self.store = None

    def snapshot(self):
        return {}

    def restore(self, _state):
        pass

    def reload_blocked(self):
        return False

    def action(self, name, payload):
        if name == "record":
            return self.store.append(payload)
        if name == "read":
            return self.store.read(payload.get("limit", 500))
        if name == "clear":
            return self.store.clear()
        raise ValueError("unknown display diagnostic action")


def create(context=None):
    return DisplayDiagnosticsModule((context or {}).get("store_factory"))
