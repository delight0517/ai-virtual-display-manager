"""OnlyChat Settings semantics against an injected, already-owned UIA window.

No global input/focus APIs are used. The caller owns desktop/PID/HWND checks and
must bound this worker process too: a hung COM provider cannot be timed out by a
polling deadline inside its own call.
"""
from __future__ import annotations

import math
import time

ARIA_PROPERTIES = 30102
ARIA_ROLE = 30101
SETTINGS_TOGGLE = "Settings (Ctrl+,)"
SETTINGS_CLOSE = "대화로 돌아가기"


class SemanticActionError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _aria_properties(raw):
    """Parse UIA's escaped key=value; collection, rejecting duplicate keys."""
    if not isinstance(raw, str):
        raise SemanticActionError("UIA_ARIA_UNREADABLE")
    result, key, value = {}, [], []
    in_value = False
    escaped = False

    def finish():
        nonlocal key, value, in_value
        if not key and not value and not in_value:
            return
        name = "".join(key)
        if not in_value or not name or name in result:
            raise SemanticActionError("UIA_ARIA_MALFORMED")
        result[name] = "".join(value)
        key, value, in_value = [], [], False

    for char in raw:
        target = value if in_value else key
        if escaped:
            if char not in "\\;=":
                raise SemanticActionError("UIA_ARIA_MALFORMED")
            target.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ";":
            finish()
        elif char == "=" and not in_value:
            in_value = True
        else:
            target.append(char)
    if escaped:
        raise SemanticActionError("UIA_ARIA_MALFORMED")
    finish()
    return result


def _property(control, property_id):
    value = control.element_info.element.GetCurrentPropertyValue(property_id)
    if not isinstance(value, str):
        raise SemanticActionError("UIA_ARIA_UNREADABLE")
    return value


def _expand_pattern(control):
    try:
        return control.iface_expand_collapse
    except Exception as error:
        if isinstance(error, AttributeError) or type(error).__name__ == "NoPatternInterfaceError":
            return None
        raise SemanticActionError("UIA_STATE_UNREADABLE") from error


def _button_state(control):
    pattern = _expand_pattern(control)
    pattern_state = None
    if pattern is not None:
        raw = pattern.CurrentExpandCollapseState
        if not isinstance(raw, int) or isinstance(raw, bool) or raw not in (0, 1):
            raise SemanticActionError("UIA_EXPANSION_UNSUPPORTED")
        pattern_state = {0: "Collapsed", 1: "Expanded"}[raw]
    aria = _aria_properties(_property(control, ARIA_PROPERTIES))
    aria_state = None
    if "expanded" in aria:
        if aria["expanded"] not in {"true", "false"}:
            raise SemanticActionError("UIA_EXPANSION_UNSUPPORTED")
        aria_state = "Expanded" if aria["expanded"] == "true" else "Collapsed"
    if pattern_state and aria_state and pattern_state != aria_state:
        raise SemanticActionError("UIA_STATE_CONFLICT")
    state = pattern_state or aria_state
    if state is None:
        raise SemanticActionError("UIA_EXPANSION_UNSUPPORTED")
    return state, pattern


def _observe(window):
    # Never retain provider elements across an action or a wait iteration.
    controls = list(window.descendants())
    toggles = [c for c in controls if c.element_info.control_type == "Button"
               and c.window_text() == SETTINGS_TOGGLE]
    if len(toggles) != 1:
        raise SemanticActionError("UIA_SETTINGS_TOGGLE_AMBIGUOUS")
    toggle = toggles[0]
    state, pattern = _button_state(toggle)
    regions = []
    for control in controls:
        if control.window_text() != "Settings":
            continue
        role = _property(control, ARIA_ROLE)
        if role == "region" or (not role and control.element_info.control_type in {"Group", "Pane"}):
            regions.append(control)
    if len(regions) > 1:
        raise SemanticActionError("UIA_SETTINGS_REGION_AMBIGUOUS")
    region_visible = False
    if regions:
        aria = _aria_properties(_property(regions[0], ARIA_PROPERTIES))
        if "hidden" in aria and aria["hidden"] not in {"true", "false"}:
            raise SemanticActionError("UIA_REGION_UNSUPPORTED")
        # aria-hidden removes the region from Chromium's accessibility tree.
        # Some providers retain it with an explicit hidden state. Either is OK;
        # global IsOffscreen is deliberately not used on a hidden desktop.
        region_visible = aria.get("hidden") != "true"
    return {"state": state, "agrees": region_visible == (state == "Expanded"),
            "toggle": toggle, "pattern": pattern, "controls": controls}


def apply_onlychat_settings(window, desired_state, *, timeout_seconds=3.0,
                            poll_seconds=0.05, clock=time.monotonic,
                            sleep=time.sleep, cancelled=lambda: False):
    """Apply Expanded/Collapsed and verify state plus Settings region transition.

    Returns a truthful performed/no-op receipt; raises SemanticActionError for
    unsupported, conflicting, ambiguous, cancelled, or unverified operations.
    Caller-supplied window must already be bound to its owned private desktop.
    """
    if desired_state not in {"Expanded", "Collapsed"}:
        raise SemanticActionError("UIA_SETTINGS_TARGET_INVALID")
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 5
            or isinstance(poll_seconds, bool) or not isinstance(poll_seconds, (int, float))
            or not math.isfinite(poll_seconds) or not 0 < poll_seconds <= timeout_seconds):
        raise SemanticActionError("UIA_SETTINGS_WAIT_INVALID")

    def check_cancelled():
        if cancelled():
            raise SemanticActionError("UIA_SETTINGS_CANCELLED")

    def receipt(performed):
        return {"performed": performed, "already_satisfied": not performed,
                "status_verified": True,
                "postcondition": {"SettingsState": desired_state}}

    try:
        check_cancelled()
        deadline = clock() + timeout_seconds
        before = _observe(window)
        if not before["agrees"]:
            raise SemanticActionError("UIA_STATE_CONFLICT")
        check_cancelled()
        if clock() >= deadline:
            raise SemanticActionError("UIA_SETTINGS_TIMEOUT")
        if before["state"] == desired_state:
            return receipt(False)
        if desired_state == "Expanded":
            if before["pattern"] is not None:
                before["pattern"].Expand()
            else:
                before["toggle"].invoke()
        else:
            close = [c for c in before["controls"]
                     if c.element_info.control_type == "Button"
                     and c.window_text() == SETTINGS_CLOSE]
            if len(close) != 1:
                raise SemanticActionError("UIA_SETTINGS_CLOSE_AMBIGUOUS")
            close[0].invoke()
        while True:
            check_cancelled()
            if clock() >= deadline:
                raise SemanticActionError("UIA_SETTINGS_TIMEOUT")
            after = _observe(window)
            check_cancelled()
            if clock() >= deadline:
                raise SemanticActionError("UIA_SETTINGS_TIMEOUT")
            if after["state"] == desired_state and after["agrees"]:
                return receipt(True)
            sleep(min(poll_seconds, max(0, deadline - clock())))
    except SemanticActionError:
        raise
    except Exception as error:
        # No provider message or user content is exposed in the error receipt.
        raise SemanticActionError("UIA_SETTINGS_PROVIDER_FAILED") from error
