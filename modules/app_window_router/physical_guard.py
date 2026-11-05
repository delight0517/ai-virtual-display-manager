"""Opt-in, fail-closed correction for newly visible off-screen app windows."""
from __future__ import annotations

import threading
import time
from pathlib import PureWindowsPath


POLL_SECONDS = 0.35
MAX_SEEN = 4096
MARGIN = 24


def _diagnostic_app(window):
    label = (window.get("app_label") or window.get("app") or "").casefold()
    if label == "only chat" or window.get("ai_task_owned") is True:
        return None
    if window.get("class_name") in {"Progman", "WorkerW", "Shell_TrayWnd",
                                    "Shell_SecondaryTrayWnd", "EdgeUiInputTopWndClass"}:
        return None
    name = PureWindowsPath(str(window.get("exe_path") or "")).name.casefold()
    if name == "discord.exe":
        return "discord"
    if name in {"firefox.exe", "chrome.exe", "msedge.exe", "brave.exe", "opera.exe"}:
        return "browser"
    if name == "explorer.exe":
        return "explorer"
    if name == "focusfense.exe":
        return "focusfense"
    return "other"


def _diagnostic_output(window, topology):
    if not isinstance(topology, dict) or topology.get("topology_verified") is not True:
        return "unknown"
    if window.get("window_state") == "minimized" or window.get("bounds_space") == "workspace":
        return "unknown"
    try:
        left, top, right, bottom = map(int, window["bounds"])
    except (TypeError, ValueError, KeyError):
        return "unknown"
    if right <= left or bottom <= top:
        return "unknown"
    displays = topology.get("displays", []) if isinstance(topology, dict) else []
    verified = [display for display in displays if display.get("attached") is True
                and display.get("verified") is True and display.get("topology_verified") is True
                and display.get("monitor_identity_verified") is True and display.get("cloned") is False]
    rect = [left, top, right, bottom]
    contained = [display for display in verified if _fully_inside(rect, display)]
    if len(contained) == 1:
        display = contained[0]
        if display.get("virtual") is True:
            return "virtual"
        if display.get("virtual") is False:
            return "primary" if int(display.get("x", 1)) == 0 and int(display.get("y", 1)) == 0 else "physical_secondary"
    if any(_intersects(rect, display) for display in displays):
        return "unknown"
    return "offscreen"


def _diagnostic_topology(topology):
    displays = topology.get("displays", []) if isinstance(topology, dict) else []
    verified = [display for display in displays if display.get("attached") is True
                and display.get("verified") is True and display.get("topology_verified") is True
                and display.get("monitor_identity_verified") is True and display.get("cloned") is False]
    physical = any(display.get("virtual") is False for display in verified)
    virtual = any(display.get("virtual") is True for display in verified)
    kind = ("physical_and_virtual" if physical and virtual else "physical_only" if physical
            else "virtual_only" if virtual else "unknown")
    verified_topology = topology.get("topology_verified") is True
    refresh_state = topology.get("monitor_identity_refresh_state")
    error_type = topology.get("monitor_identity_error_type")
    if verified_topology:
        reason = None
    elif topology.get("provider_failure") is True:
        reason = "topology_provider_exception"
    elif refresh_state in {"refreshing", "pending"}:
        reason = "monitor_identity_refresh_pending"
    elif refresh_state in {"error", "unavailable", "timeout"} or error_type:
        reason = "monitor_identity_refresh_failed"
    elif topology.get("display_path_error_type"):
        reason = "display_path_query_failed"
    else:
        reason = "monitor_identity_unverified"
    result = {"topology": kind, "output_count": min(len(verified), 16),
              "result": "succeeded" if verified_topology else "unavailable",
              "reason": reason}
    # These bounded fields explain transient failures without recording monitor
    # paths, device IDs, raw Windows errors, or exception messages.
    if refresh_state in {"fresh", "refreshing", "pending", "error", "unavailable", "timeout"}:
        result["monitor_identity_refresh_state"] = refresh_state
    if isinstance(topology.get("monitor_identity_age_ms"), int):
        result["monitor_identity_age_ms"] = max(0, min(topology["monitor_identity_age_ms"], 86_400_000))
    if isinstance(topology.get("monitor_identity_cache_hit"), bool):
        result["monitor_identity_cache_hit"] = topology["monitor_identity_cache_hit"]
    result["display_count"] = min(len(displays), 16)
    result["identified_display_count"] = min(sum(1 for item in displays
                                                  if item.get("monitor_identity_verified") is True), 16)
    if topology.get("provider_failure") is True:
        result["provider_error_type"] = topology.get("provider_error_type", "Other")
    return result


def _fully_inside(bounds, display):
    left, top, right, bottom = map(int, bounds)
    x, y = int(display["x"]), int(display["y"])
    return (left >= x and top >= y and right <= x + int(display["width"])
            and bottom <= y + int(display["height"]))


def _intersects(bounds, display):
    left, top, right, bottom = map(int, bounds)
    x, y = int(display["x"]), int(display["y"])
    return left < x + int(display["width"]) and right > x and top < y + int(display["height"]) and bottom > y


def correction_target(window, topology):
    """Return a safe primary-physical rectangle, or None when evidence is ambiguous."""
    if not isinstance(topology, dict) or topology.get("topology_verified") is not True:
        return None
    # The user's ordinary Codex/ChatGPT window is a user app. Only explicit
    # task ownership (and the separately managed Only Chat host) excludes it.
    if (window.get("app_label") or window.get("app")) == "Only Chat" or window.get("ai_task_owned") is True:
        return None
    if window.get("class_name") in {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "EdgeUiInputTopWndClass", "ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"}:
        return None
    displays = topology.get("displays")
    if not isinstance(displays, list) or not displays:
        return None
    mtt_instance = str(topology.get("mtt_device_instance_id", "")).strip().casefold()
    if not mtt_instance:
        return None
    if window.get("window_state") != "normal" or window.get("bounds_space") != "screen":
        return None
    try:
        left, top, right, bottom = map(int, window["bounds"])
    except (TypeError, ValueError, KeyError):
        return None
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None
    # Do not touch an app already wholly on any physical output.
    physical = [d for d in displays if d.get("virtual") is False and d.get("attached") is True
                and d.get("verified") is True and d.get("cloned") is False
                and d.get("topology_verified") is True and d.get("monitor_identity_verified") is True
                and d.get("monitor_parent")
                and str(d.get("monitor_parent", "")).strip().casefold() != mtt_instance]
    if any(_fully_inside(window["bounds"], d) for d in physical):
        return None
    # Correct only windows fully on the verified virtual output or wholly off every output.
    virtual = [d for d in displays if d.get("virtual") is True and d.get("attached") is True
               and d.get("verified") is True and d.get("cloned") is False
               and d.get("topology_verified") is True and d.get("monitor_identity_verified") is True
               and str(d.get("monitor_parent", "")).strip().casefold() == mtt_instance]
    on_virtual = any(_fully_inside(window["bounds"], d) for d in virtual)
    # Native frames can extend a few pixels across an output edge. Recover only
    # overwhelmingly virtual placement; ordinary cross-monitor user layouts stay put.
    def virtual_share(display):
        x, y = int(display["x"]), int(display["y"])
        overlap = max(0, min(right, x + int(display["width"])) - max(left, x)) * max(0, min(bottom, y + int(display["height"])) - max(top, y))
        return overlap / (width * height)
    on_virtual = on_virtual or any(virtual_share(d) >= 0.90 for d in virtual)
    intersects_any = any(_intersects(window["bounds"], d) for d in displays)
    if not on_virtual and intersects_any:
        return None
    primaries = [d for d in physical if int(d.get("x", 1)) == 0 and int(d.get("y", 1)) == 0]
    if not primaries and len(physical) == 1:
        primaries = physical  # A virtual primary must not hide the sole real screen.
    if len(primaries) != 1:
        return None
    target = primaries[0]
    max_width = int(target["width"]) - MARGIN * 2
    max_height = int(target["height"]) - MARGIN * 2
    if max_width <= 0 or max_height <= 0:
        return None
    width, height = min(width, max_width), min(height, max_height)
    x, y = int(target["x"]) + MARGIN, int(target["y"]) + MARGIN
    return {"display": target, "bounds": [x, y, x + width, y + height]}


def topology_ready(topology):
    if not isinstance(topology, dict) or topology.get("topology_verified") is not True:
        return False
    displays = topology.get("displays", [])
    mtt_instance = str(topology.get("mtt_device_instance_id", "")).strip().casefold()
    if not mtt_instance:
        return False
    physical = [d for d in displays if d.get("virtual") is False and d.get("attached") is True
                and d.get("verified") is True and d.get("cloned") is False
                and d.get("topology_verified") is True and d.get("monitor_identity_verified") is True
                and d.get("monitor_parent")
                and str(d.get("monitor_parent", "")).strip().casefold() != mtt_instance]
    virtual = [d for d in displays if d.get("virtual") is True and d.get("attached") is True
               and d.get("verified") is True and d.get("cloned") is False
               and d.get("topology_verified") is True and d.get("monitor_identity_verified") is True
               and str(d.get("monitor_parent", "")).strip().casefold() == mtt_instance]
    return (len(physical) == 1 or len([d for d in physical
            if int(d.get("x", 1)) == 0 and int(d.get("y", 1)) == 0]) == 1)


class PhysicalWindowGuard:
    """Polls window metadata only while explicitly enabled; callbacks are injected."""
    def __init__(self, window_api, topology_provider, interval=POLL_SECONDS,
                 diagnostic_recorder=None):
        self.window_api = window_api
        self.topology_provider = topology_provider
        self.interval = max(0.1, float(interval))
        self.diagnostic_recorder = diagnostic_recorder
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._seen = {}
        self._diagnostic_observations = {}
        self._last_foreground = None
        self._activation_pending = None
        self._topology_cache = None
        self._topology_at = 0.0
        self._topology_signature = None
        self._status = {"enabled": False, "state": "off", "moved_count": 0, "error": ""}

    def start(self, recover_existing=False):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return dict(self._status)
            self._stop.clear()
            self._seen.clear()
            self._diagnostic_observations.clear()
            self._topology_signature = None
            self._topology_cache = None
            # Baseline existing windows; only later new identities can move.
            rows = self.window_api.enumerate_all_windows()
            self._last_foreground = next((str(w["window_id"]) for w in rows if w.get("foreground")), None)
            self._seen.update({str(w["window_id"]): ("pending" if recover_existing else "baseline") for w in rows
                               if w.get("window_id")})
            self._status = {"enabled": True, "state": "running", "moved_count": 0, "error": ""}
            self._record({"event": "guard_state", "enabled": True, "result": "started"})
            self._thread = threading.Thread(target=self._run, name="physical-window-guard", daemon=True)
            self._thread.start()
            return dict(self._status)

    def stop(self, timeout=3.0):
        # Signal first, without waiting for an in-flight nonactivating SetWindowPos.
        # The join is the bounded proof that no later window moves can occur.
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(max(0.0, float(timeout)))
        alive = bool(thread and thread.is_alive())
        if alive:
            # Avoid blocking on the worker's move-serialization lock after timeout.
            previous = self._status
            self._status = {"enabled": True, "state": "stop_unverified",
                            "moved_count": previous.get("moved_count", 0), "error": "stop timeout"}
            self._record({"event": "guard_state", "enabled": True, "result": "failed",
                          "reason": "timeout"})
        else:
            with self._lock:
                self._status = {"enabled": False, "state": "off",
                                "moved_count": self._status.get("moved_count", 0), "error": ""}
                self._thread = None
            self._record({"event": "guard_state", "enabled": False, "result": "closed"})
        return dict(self._status)

    def status(self):
        with self._lock:
            return dict(self._status)

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                topology = self._topology()
                self._record_topology(topology)
                signature = tuple(sorted(
                    (str(d.get("monitor_path", "")), str(d.get("display_name", "")),
                     d.get("x"), d.get("y"), d.get("width"), d.get("height"))
                    for d in topology.get("displays", [])))
                if not topology_ready(topology):
                    self._topology_signature = signature
                    self._status.update(state="waiting_for_physical_monitor", error="")
                    continue
                # This guard cannot act until a verified physical output exists.
                # Avoid repeatedly enumerating every top-level window while it is
                # waiting; newly opened windows are discovered on the first ready
                # cycle below.
                rows = self.window_api.enumerate_all_windows()
                if self._topology_signature is not None and signature != self._topology_signature:
                    # Reconnection can strand windows that were already handled
                    # while the physical monitor was absent. Recheck once.
                    self._seen = {str(w["window_id"]): "pending" for w in rows if w.get("window_id")}
                self._topology_signature = signature
                self._status.update(state="running", error="")
                foreground = next((str(w["window_id"]) for w in rows if w.get("foreground")), None)
                activated = foreground if foreground != self._last_foreground else None
                self._last_foreground = foreground
                if activated:
                    self._activation_pending = activated
                for row in rows:
                    identity = str(row.get("window_id") or "")
                    if not identity:
                        continue
                    if identity not in self._seen:
                        if len(self._seen) >= MAX_SEEN:
                            self._status.update(state="capacity_reached", error="window ledger full")
                            self._stop.set()
                            return
                        self._seen[identity] = "pending"
                    # Windows that existed when the user enabled the guard never move,
                    # including if they are later restored from minimized state.
                    is_new = self._seen[identity] == "pending"
                    is_activation = identity == self._activation_pending
                    if not is_new and not is_activation:
                        continue
                    category = _diagnostic_app(row)
                    output = _diagnostic_output(row, topology)
                    if category and output in {"virtual", "offscreen", "unknown"}:
                        self._record_window("window_observed", row, category, output,
                                            "window_minimized" if output == "unknown" else None)
                    if row.get("window_state") == "minimized" or row.get("bounds_space") == "workspace":
                        continue
                    # A newly opened window gets exactly one placement decision once
                    # it has normal screen-space bounds. User dragging it later is respected.
                    self._seen[identity] = "handled"
                    target = correction_target(row, topology)
                    if target is None:
                        if is_activation:
                            self._activation_pending = None
                        continue
                    if category:
                        self._record_window("restore_requested", row, category, output,
                                            bounds=row.get("bounds"))
                    # Serialize a placement with stop(): once stop() acquires this
                    # lock, no later placement can begin.
                    with self._lock:
                        if self._stop.is_set():
                            return
                        topology = self._topology(force=True)
                        target = correction_target(row, topology)
                        if target is None:
                            if category:
                                self._record_window("restore_result", row, category, output,
                                                    result="skipped", reason="topology_unverified")
                            if is_activation:
                                self._activation_pending = None
                            continue
                        if not self.window_api.move_exact_without_activation(row, target["bounds"]):
                            if category:
                                self._record_window("restore_result", row, category, output,
                                                    result="failed", reason="api_failed")
                            if is_activation:
                                self._activation_pending = None
                            continue
                    # Verify the exact process/window identity and destination after placement.
                    after = [w for w in self.window_api.enumerate_all_windows()
                             if w.get("window_id") == identity and w.get("hwnd") == row.get("hwnd")]
                    latest = self._topology(force=True)
                    latest_target = correction_target(row, latest)
                    identity_keys = ("display_name", "adapter_device_id", "monitor_device_id", "monitor_path")
                    same_monitor = bool(latest_target and all(
                        target["display"].get(key)
                        and str(latest_target["display"].get(key, "")).casefold()
                        == str(target["display"].get(key, "")).casefold()
                        for key in identity_keys))
                    if (len(after) == 1 and _fully_inside(after[0].get("bounds", []), target["display"])
                            and same_monitor):
                        with self._lock:
                            self._status["moved_count"] += 1
                        if category:
                            self._record_window("restore_result", row, category, "primary",
                                                result="succeeded", bounds=after[0]["bounds"])
                    elif category:
                        self._record_window("restore_result", row, category, output,
                                            result="failed", reason="identity_changed")
                    if is_activation:
                        self._activation_pending = None
            except Exception as exc:
                with self._lock:
                    self._status.update(state="error", error=type(exc).__name__)
                self._record_topology({"topology_verified": False, "displays": []})
                # A hotplug probe failure must not silently kill persisted
                # protection. No move occurs on this cycle; retry discovery.
                self._topology_cache = None
                self._topology_signature = ()

    def _record(self, event):
        if not callable(self.diagnostic_recorder):
            return
        try:
            self.diagnostic_recorder(event)
        except Exception as exc:
            with self._lock:
                self._status["diagnostics_error"] = type(exc).__name__

    def _record_topology(self, topology):
        summary = _diagnostic_topology(topology)
        signature = tuple(summary.get(key) for key in ("topology", "output_count", "result", "reason"))
        if signature == getattr(self, "_last_topology_signature", None):
            return
        self._last_topology_signature = signature
        self._record({"event": "topology_state", **summary})

    def _record_window(self, event, row, category, output, reason=None,
                       result=None, bounds=None):
        payload = {"event": event, "app": category,
                   "window_state": row.get("window_state", "unknown"), "output": output}
        if reason:
            payload["reason"] = reason
        if result:
            payload["result"] = result
        if bounds is not None and row.get("bounds_space") == "screen":
            payload["bounds"] = bounds
        identity = str(row.get("window_id") or "")
        signature = (event, category, payload["window_state"], output, reason, result,
                     tuple(payload.get("bounds", ())))
        if identity and self._diagnostic_observations.get(identity) == signature:
            return
        if identity:
            self._diagnostic_observations[identity] = signature
        self._record(payload)

    def _topology(self, force=False):
        now = time.monotonic()
        if not force and self._topology_cache is not None and now - self._topology_at < 2.0:
            return self._topology_cache
        value = self.topology_provider()
        self._topology_cache = value
        self._topology_at = now
        return value


__all__ = ["PhysicalWindowGuard", "correction_target", "topology_ready"]
