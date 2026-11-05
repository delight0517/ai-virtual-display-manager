"""Fail-closed, ownership-aware cursor clipping for a verified physical output.

This module is deliberately inert until an owner explicitly calls ``start``.
The Win32 calls are injected so source QA never clips the user's real cursor.
"""
from __future__ import annotations

import threading


def _rect_from_display(display):
    try:
        values = (display["x"], display["y"], display["width"], display["height"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if any(type(value) is not int for value in values):
        return None
    x, y, width, height = values
    if width <= 0 or height <= 0:
        return None
    return (x, y, x + width, y + height)


def _contains(outer, inner):
    return (outer[0] <= inner[0] and outer[1] <= inner[1]
            and outer[2] >= inner[2] and outer[3] >= inner[3])


def _topology_snapshot(topology):
    """Return exact physical bounds and an endpoint signature, or fail closed."""
    if not isinstance(topology, dict) or topology.get("topology_verified") is not True:
        return None
    mtt_id = str(topology.get("mtt_device_instance_id", "")).strip().casefold()
    displays = topology.get("displays")
    if not mtt_id or not isinstance(displays, list) or not displays:
        return None
    verified = []
    for display in displays:
        if not isinstance(display, dict):
            return None
        if (display.get("attached") is not True
                or display.get("verified") is not True
                or display.get("topology_verified") is not True
                or display.get("monitor_identity_verified") is not True
                or display.get("cloned") is not False
                or type(display.get("clone_source_count")) is not int
                or display.get("clone_source_count") != 1
                or not str(display.get("monitor_path", "")).strip()):
            return None
        rect = _rect_from_display(display)
        if rect is None:
            return None
        parent = str(display.get("monitor_parent", "")).strip().casefold()
        is_virtual = display.get("virtual")
        if type(is_virtual) is not bool or not parent:
            return None
        if is_virtual and parent != mtt_id:
            return None
        if not is_virtual and parent == mtt_id:
            return None
        identity_fields = ("display_name", "adapter", "adapter_device_id",
                           "monitor_device_id", "target_name")
        identities = tuple(str(display.get(key, "")).strip().casefold()
                           for key in identity_fields)
        if any(not identity for identity in identities):
            return None
        signature = (identities, str(display["monitor_path"]).strip().casefold(),
                     parent, rect, is_virtual)
        verified.append((display, rect, signature))
    physical = [(display, rect, sig) for display, rect, sig in verified
                if display["virtual"] is False]
    virtual = [display for display, _, _ in verified if display["virtual"] is True]
    if len(physical) != 1 or not virtual:
        return None
    display, physical_rect, physical_signature = physical[0]
    # Avoid a clip whose rectangle could include another active output.
    if any(_intersects(physical_rect, rect) for other, rect, _ in verified
           if other is not display):
        return None
    desktop_rect = (min(rect[0] for _, rect, _ in verified),
                    min(rect[1] for _, rect, _ in verified),
                    max(rect[2] for _, rect, _ in verified),
                    max(rect[3] for _, rect, _ in verified))
    return physical_rect, desktop_rect, (mtt_id, physical_signature,
                                         tuple(sorted(sig for _, _, sig in verified)))


def _intersects(left, right):
    return (left[0] < right[2] and left[2] > right[0]
            and left[1] < right[3] and left[3] > right[1])


class VerifiedCursorClip:
    """Apply one verified physical-output clip without taking another owner's clip."""

    def __init__(self, topology_reader, clip_reader, clip_writer):
        """Readers/writer use screen-pixel tuples; ``None`` means unclipped."""
        if not all(callable(item) for item in
                   (topology_reader, clip_reader, clip_writer)):
            raise TypeError("Topology and cursor clip readers/writer are required")
        self._topology_reader = topology_reader
        self._clip_reader = clip_reader
        self._clip_writer = clip_writer
        self._lock = threading.RLock()
        self._owned_rect = None
        self._signature = None

    def start(self):
        with self._lock:
            try:
                snapshot = _topology_snapshot(self._topology_reader())
            except Exception:
                snapshot = None
            if snapshot is None:
                return {"state": "failed", "error_code": "TOPOLOGY_UNVERIFIED"}
            physical_rect, desktop_rect, signature = snapshot
            try:
                current = self._clip_reader()
            except Exception:
                return {"state": "failed", "error_code": "CURSOR_CLIP_READ_FAILED"}
            if self._owned_rect is not None:
                if current == self._owned_rect and signature == self._signature:
                    return {"state": "running", "bounds": list(self._owned_rect)}
                return {"state": "failed", "error_code": "OWNED_CLIP_CHANGED"}
            if current is not None:
                if (not isinstance(current, (tuple, list)) or len(current) != 4
                        or any(type(value) is not int for value in current)):
                    return {"state": "failed", "error_code": "CURSOR_CLIP_INVALID"}
                current = tuple(current)
                if _contains(physical_rect, current):
                    return {"state": "preserved", "reason": "EXTERNAL_CLIP_ALREADY_SAFE",
                            "bounds": list(current)}
                if current != desktop_rect:
                    return {"state": "failed", "error_code": "EXTERNAL_CLIP_CONFLICT"}
            try:
                applied = self._clip_writer(physical_rect) is True
            except Exception:
                applied = False
            if not applied:
                try:
                    possibly_owned = self._clip_reader()
                    if possibly_owned == physical_rect:
                        rollback_ok = self._clip_writer(None) is True
                        if not rollback_ok or self._clip_reader() is not None:
                            return {"state": "failed",
                                    "error_code": "CURSOR_CLIP_ROLLBACK_UNVERIFIED"}
                except Exception:
                    return {"state": "failed",
                            "error_code": "CURSOR_CLIP_ROLLBACK_UNVERIFIED"}
                return {"state": "failed", "error_code": "CURSOR_CLIP_APPLY_FAILED"}
            try:
                after_snapshot = _topology_snapshot(self._topology_reader())
                after_clip = self._clip_reader()
            except Exception:
                after_snapshot, after_clip = None, None
            if (after_snapshot is None or after_snapshot[2] != signature
                    or after_clip != physical_rect):
                # Roll back only if our exact rectangle remains installed.
                if after_clip == physical_rect:
                    try:
                        rollback_ok = self._clip_writer(None) is True
                        rollback_verified = rollback_ok and self._clip_reader() is None
                    except Exception:
                        rollback_verified = False
                    if not rollback_verified:
                        return {"state": "failed",
                                "error_code": "CURSOR_CLIP_ROLLBACK_UNVERIFIED"}
                return {"state": "failed", "error_code": "CURSOR_CLIP_VERIFY_FAILED"}
            self._owned_rect = physical_rect
            self._signature = signature
            return {"state": "running", "bounds": list(physical_rect),
                    "topology_verified": True}

    def stop(self):
        with self._lock:
            if self._owned_rect is None:
                return {"state": "stopped", "restored": True, "owned_clip": False}
            try:
                current = self._clip_reader()
            except Exception:
                return {"state": "failed", "error_code": "CURSOR_CLIP_READ_FAILED"}
            if current != self._owned_rect:
                return {"state": "failed", "error_code": "OWNED_CLIP_CHANGED"}
            try:
                restored = self._clip_writer(None) is True
                after = self._clip_reader()
            except Exception:
                restored, after = False, self._owned_rect
            if not restored or after is not None:
                return {"state": "failed", "error_code": "CURSOR_CLIP_RESTORE_UNVERIFIED"}
            self._owned_rect = None
            self._signature = None
            return {"state": "stopped", "restored": True, "owned_clip": True}

    def status(self):
        with self._lock:
            return {"state": "running" if self._owned_rect is not None else "off",
                    "bounds": list(self._owned_rect) if self._owned_rect is not None else None}


__all__ = ["VerifiedCursorClip"]
