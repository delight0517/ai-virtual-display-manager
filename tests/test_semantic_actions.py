import unittest
from types import SimpleNamespace

from modules.private_desktop.semantic_actions import (
    ARIA_PROPERTIES, ARIA_ROLE, SETTINGS_TOGGLE, SETTINGS_CLOSE,
    SemanticActionError, _aria_properties, apply_onlychat_settings,
)


class Control:
    def __init__(self, name, kind="Button", aria="", role="", callback=None, pattern=None):
        self.name, self.aria, self.role = name, aria, role
        self.callback = callback
        self.element_info = SimpleNamespace(control_type=kind, element=self)
        if pattern is not None:
            self.iface_expand_collapse = pattern

    def window_text(self):
        return self.name

    def GetCurrentPropertyValue(self, key):
        return self.aria if key == ARIA_PROPERTIES else self.role if key == ARIA_ROLE else None

    def invoke(self):
        if self.callback:
            self.callback()


class FakeWindow:
    def __init__(self):
        self.expanded = False
        self.region = False
        self.invokes = 0
        self.pattern = False
        self.no_state = False
        self.pattern_conflict = False
        self.duplicate_toggle = False
        self.duplicate_region = False
        self.close_present = True
        self.action_noop = False
        self.aria_suffix = ""
        self.reads = 0

    def change(self, expanded):
        self.invokes += 1
        if not self.action_noop:
            self.expanded = self.region = expanded

    def descendants(self):
        self.reads += 1
        aria = "" if self.no_state else f"expanded={str(self.expanded).lower()}"
        pattern = None
        if self.pattern:
            pattern = SimpleNamespace(CurrentExpandCollapseState=
                int(not self.expanded if self.pattern_conflict else self.expanded),
                Expand=lambda: self.change(True))
        toggle = Control(SETTINGS_TOGGLE, aria=aria + self.aria_suffix,
                         callback=lambda: self.change(True), pattern=pattern)
        controls = [toggle]
        if self.duplicate_toggle:
            controls.append(toggle)
        if self.region:
            region = Control("Settings", "Group", role="region", aria="hidden=false")
            controls.append(region)
            if self.duplicate_region:
                controls.append(region)
        if self.expanded and self.close_present:
            controls.append(Control(SETTINGS_CLOSE, callback=lambda: self.change(False)))
        return controls


class SemanticActionsTests(unittest.TestCase):
    def setUp(self):
        self.window = FakeWindow()
        self.now = 0

    def apply(self, state, **kwargs):
        return apply_onlychat_settings(self.window, state, timeout_seconds=.2,
            poll_seconds=.05, clock=lambda: self.now,
            sleep=lambda seconds: setattr(self, "now", self.now + seconds), **kwargs)

    def test_open_and_inverse_close_verify_fresh_region_and_state(self):
        opened = self.apply("Expanded")
        self.assertTrue(opened["performed"])
        self.assertEqual(opened["postcondition"], {"SettingsState": "Expanded"})
        self.assertTrue(self.window.region)
        self.assertGreaterEqual(self.window.reads, 2)
        self.assertTrue(self.apply("Collapsed")["status_verified"])
        self.assertFalse(self.window.region)
        self.assertEqual(self.window.invokes, 2)

    def test_supported_expand_pattern_and_exact_aria_must_agree(self):
        self.window.pattern = True
        self.assertTrue(self.apply("Expanded")["performed"])
        self.window.pattern_conflict = True
        with self.assertRaisesRegex(SemanticActionError, "UIA_STATE_CONFLICT"):
            self.apply("Collapsed")
        self.assertEqual(self.window.invokes, 1)

    def test_already_satisfied_is_verified_explicit_noop(self):
        result = self.apply("Collapsed")
        self.assertFalse(result["performed"])
        self.assertTrue(result["already_satisfied"])
        self.assertTrue(result["status_verified"])
        self.assertEqual(self.window.invokes, 0)

    def test_unchanged_name_and_successful_invoke_are_not_proof(self):
        self.window.action_noop = True
        with self.assertRaisesRegex(SemanticActionError, "UIA_SETTINGS_TIMEOUT"):
            self.apply("Expanded")

    def test_pattern_state_without_region_agreement_is_not_success(self):
        self.window.pattern = True
        self.window.expanded = True
        with self.assertRaisesRegex(SemanticActionError, "UIA_STATE_CONFLICT"):
            self.apply("Expanded")
        self.assertEqual(self.window.invokes, 0)

    def test_missing_state_and_ambiguous_controls_fail_without_action(self):
        for field, error in [("no_state", "UIA_EXPANSION_UNSUPPORTED"),
                             ("duplicate_toggle", "UIA_SETTINGS_TOGGLE_AMBIGUOUS")]:
            self.window = FakeWindow()
            setattr(self.window, field, True)
            with self.subTest(field=field), self.assertRaisesRegex(SemanticActionError, error):
                self.apply("Expanded")
            self.assertEqual(self.window.invokes, 0)

    def test_duplicate_region_and_missing_inverse_are_rejected(self):
        self.window.expanded = self.window.region = True
        self.window.duplicate_region = True
        with self.assertRaisesRegex(SemanticActionError, "UIA_SETTINGS_REGION_AMBIGUOUS"):
            self.apply("Collapsed")
        self.window.duplicate_region = False
        self.window.close_present = False
        with self.assertRaisesRegex(SemanticActionError, "UIA_SETTINGS_CLOSE_AMBIGUOUS"):
            self.apply("Collapsed")
        self.assertEqual(self.window.invokes, 0)

    def test_aria_parser_preserves_escaped_delimiters_and_rejects_duplicates(self):
        self.assertEqual(_aria_properties(r"label=a\;b\=c\\d;expanded=true;"),
                         {"label": r"a;b=c\d", "expanded": "true"})
        for raw in ["expanded=true;expanded=false", "expanded", "expanded=true\\",
                    r"label=bad\q;expanded=true", "=true"]:
            with self.subTest(raw=raw), self.assertRaises(SemanticActionError):
                _aria_properties(raw)

    def test_partial_match_cannot_spoof_expanded_state(self):
        self.window.no_state = True
        self.window.aria_suffix = "notexpanded=true;label=expanded\\=true"
        with self.assertRaisesRegex(SemanticActionError, "UIA_EXPANSION_UNSUPPORTED"):
            self.apply("Expanded")

    def test_cancel_before_action_and_during_wait_never_returns_success(self):
        with self.assertRaisesRegex(SemanticActionError, "UIA_SETTINGS_CANCELLED"):
            self.apply("Expanded", cancelled=lambda: True)
        self.assertEqual(self.window.invokes, 0)
        self.window.action_noop = True
        with self.assertRaisesRegex(SemanticActionError, "UIA_SETTINGS_CANCELLED"):
            self.apply("Expanded", cancelled=lambda: self.window.invokes > 0)

    def test_late_region_transition_is_polled_with_fresh_elements(self):
        original = self.window.descendants
        def descendants():
            if self.window.invokes and self.window.reads >= 2:
                self.window.expanded = self.window.region = True
            return original()
        self.window.descendants = descendants
        self.window.action_noop = True
        self.assertTrue(self.apply("Expanded")["performed"])
        self.assertGreaterEqual(self.window.reads, 3)

    def test_malformed_target_and_wait_are_rejected(self):
        with self.assertRaises(SemanticActionError):
            self.apply("Open")
        for value in [float("nan"), -1, True, 6]:
            with self.subTest(value=value), self.assertRaises(SemanticActionError):
                apply_onlychat_settings(self.window, "Expanded", timeout_seconds=value)

    def test_reserved_unsupported_property_object_never_becomes_state(self):
        sentinel = object()
        original = self.window.descendants
        def descendants():
            controls = original()
            controls[0].aria = sentinel
            return controls
        self.window.descendants = descendants
        with self.assertRaisesRegex(SemanticActionError, "UIA_ARIA_UNREADABLE"):
            self.apply("Expanded")
        self.assertEqual(self.window.invokes, 0)

    def test_provider_returning_success_after_deadline_is_not_verified(self):
        original = self.window.descendants
        def descendants():
            if self.window.invokes:
                self.now += 1
            return original()
        self.window.descendants = descendants
        with self.assertRaisesRegex(SemanticActionError, "UIA_SETTINGS_TIMEOUT"):
            self.apply("Expanded")

    def test_waits_are_bounded_and_a_missing_region_times_out(self):
        waits = []
        original = self.window.descendants
        def descendants():
            controls = original()
            return [c for c in controls if c.window_text() != "Settings"]
        self.window.descendants = descendants
        def sleep(seconds):
            waits.append(seconds)
            self.now += seconds
        with self.assertRaisesRegex(SemanticActionError, "UIA_SETTINGS_TIMEOUT"):
            apply_onlychat_settings(self.window, "Expanded", timeout_seconds=.2,
                poll_seconds=.05, clock=lambda: self.now, sleep=sleep)
        self.assertTrue(waits)
        self.assertTrue(all(0 <= seconds <= .05 for seconds in waits))
        self.assertLessEqual(sum(waits), .2)


if __name__ == "__main__":
    unittest.main()
