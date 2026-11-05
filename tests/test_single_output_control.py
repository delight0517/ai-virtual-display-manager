import unittest
from pathlib import Path

from core.module_host import ModuleHost
from modules.single_output_control.module import (
    EXPECTED_DISPLAY_NAME,
    SingleOutputControlModule,
    validate_state,
)


def state(*, outputs=None, pool_state="off", topology_verified=True):
    return {
        "mtt_device_instance_id": r"ROOT\DISPLAY\MTTVDD",
        "topology_verified": topology_verified,
        "pool_state": pool_state,
        "outputs": list(outputs or []),
    }


def output(name=EXPECTED_DISPLAY_NAME, *, cloned=False, parent=r"ROOT\DISPLAY\MTTVDD",
           topology_verified=True):
    return {"display_name": name, "cloned": cloned,
            "monitor_parent": parent, "topology_verified": topology_verified}


class SingleOutputControlTests(unittest.TestCase):
    def make_module(self, read_state, set_count):
        module = SingleOutputControlModule()
        module.bind(read_state=read_state, set_count=set_count)
        return module

    def test_preview_only_reports_confirm_required_without_mutation(self):
        calls = []
        module = self.make_module(lambda: state(), calls.append)
        result = module.action("preview", {})
        self.assertEqual(result["state"], "confirm_required")
        self.assertEqual(calls, [])

    def test_create_requires_explicit_confirmation(self):
        calls = []
        module = self.make_module(lambda: state(), calls.append)
        with self.assertRaisesRegex(RuntimeError, "explicit in-app confirmation"):
            module.action("create", {})
        self.assertEqual(calls, [])

    def test_create_requests_exactly_one_and_rechecks_display10(self):
        states = iter([state(), state(outputs=[output()])])
        calls = []
        module = self.make_module(lambda: next(states), calls.append)
        result = module.action("create", {"confirmed": True})
        self.assertEqual(calls, [1])
        self.assertEqual(result["display_name"], EXPECTED_DISPLAY_NAME)
        self.assertEqual(result["state"], "ready")

    def test_existing_exact_noncloned_display10_is_idempotent(self):
        calls = []
        module = self.make_module(lambda: state(outputs=[output()]), calls.append)
        self.assertEqual(module.action("create", {"confirmed": True})["state"], "ready")
        self.assertEqual(calls, [])

    def test_wrong_display_name_fails_without_removal_or_second_command(self):
        states = iter([state(), state(outputs=[output(r"\\.\DISPLAY3")])])
        calls = []
        module = self.make_module(lambda: next(states), calls.append)
        with self.assertRaisesRegex(RuntimeError, "exact verified DISPLAY10"):
            module.action("create", {"confirmed": True})
        self.assertEqual(calls, [1])

    def test_clone_and_wrong_parent_fail_closed(self):
        for row in (output(cloned=True), output(parent=r"ROOT\OTHER\DEVICE")):
            with self.subTest(row=row), self.assertRaises(RuntimeError):
                validate_state(state(outputs=[row]))

    def test_unverified_topology_ambiguous_identity_and_unknown_pool_fail(self):
        cases = [state(topology_verified=False),
                 {**state(), "mtt_device_instance_id": ""},
                 state(pool_state="recovery_required"),
                 state(outputs=[output(), output()])]
        for candidate in cases:
            with self.subTest(candidate=candidate), self.assertRaises(RuntimeError):
                validate_state(candidate)

    def test_unsupported_action_rejected(self):
        module = self.make_module(lambda: state(), lambda _count: None)
        with self.assertRaises(ValueError):
            module.action("stop", {})

    def test_module_host_reload_rebinds_backend_without_touching_pool(self):
        host = ModuleHost(Path(__file__).resolve().parents[1])
        first = host.load("single_output_control")
        loaded = host._loaded["single_output_control"].instance
        counts = []
        loaded.bind(read_state=lambda: state(), set_count=counts.append)
        second = host.reload("single_output_control")
        reloaded = host._loaded["single_output_control"].instance
        reloaded.bind(read_state=lambda: state(), set_count=counts.append)
        self.assertEqual(second["generation"], first["generation"] + 1)
        self.assertEqual(reloaded.action("preview", {})["state"], "confirm_required")
        self.assertEqual(counts, [])
        host.dispose_all()

    def test_module_host_rejects_action_while_an_operation_is_in_flight(self):
        host = ModuleHost(Path(__file__).resolve().parents[1])
        host.load("single_output_control")
        host.active_operations = 1
        try:
            with self.assertRaisesRegex(RuntimeError, "already active"):
                host.invoke("single_output_control", "preview")
        finally:
            host.active_operations = 0
            host.dispose_all()


if __name__ == "__main__":
    unittest.main()
