"""Task cancellation and bounded dispatch tests; no devices or native runtime."""
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from core.module_host import ModuleHost


class HostTaskDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "module-registry.json").write_text(json.dumps({"modules": []}))
        self.host = ModuleHost(root)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.threads = []
        self.outcomes = []
        self.instance = SimpleNamespace(action=lambda name, payload: {"action": name},
                                        refresh=lambda: {"topology": "synthetic"},
                                        dispose=lambda: None, snapshot=lambda: {})
        self.definition = SimpleNamespace(
            ACTIONS=("start", "status", "cancel", "stop", "observe", "interrupt"),
            CONCURRENT_ACTIONS=("status", "cancel", "stop", "interrupt"),
            __name__="_task_dispatch_fixture")
        self.host._loaded["task"] = SimpleNamespace(
            module_id="task", module=self.definition, instance=self.instance)

    def tearDown(self):
        self.release.set()
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(), "Synthetic task thread leaked")
        self.assertEqual(self.host.active_operations, 0)
        self.assertEqual(self.host._exclusive_depth, 0)
        self.assertIsNone(self.host._operation_thread_id)
        self.host.dispose_all()
        self.temp.cleanup()

    def background(self, call):
        outcome = []
        def run():
            try:
                outcome.append(("ok", call()))
            except Exception as error:
                outcome.append(("error", error))
        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread, outcome

    def block_refresh(self):
        def refresh():
            self.entered.set()
            if not self.release.wait(3):
                raise RuntimeError("Synthetic refresh not released")
            return {"ready": True}
        self.instance.refresh = refresh
        thread, outcome = self.background(lambda: self.host.refresh("task"))
        self.assertTrue(self.entered.wait(1))
        return thread, outcome

    def test_default_invoke_rejects_promptly_while_refresh_is_blocked(self):
        self.block_refresh()
        thread, outcome = self.background(lambda: self.host.invoke("task", "start"))
        thread.join(.5)
        self.assertFalse(thread.is_alive(), "Default invoke unexpectedly waited")
        self.assertEqual(outcome[0][0], "error")
        self.assertRegex(str(outcome[0][1]), "already active")

    def test_bounded_wait_releases_lock_and_dispatches_after_refresh_finishes(self):
        refresh, first = self.block_refresh()
        wait_entered = threading.Event()
        original_wait = self.host._operation_changed.wait_for
        def observed_wait(predicate, timeout):
            wait_entered.set()
            return original_wait(predicate, timeout=timeout)
        self.host._operation_changed.wait_for = observed_wait
        waiting, second = self.background(lambda: self.host.invoke("task", "start", wait_timeout=1))
        self.assertTrue(wait_entered.wait(1), "Fixture must exercise the contended wait path")
        # A condition waiter must not prevent the original refresh's finally
        # from reacquiring the host lock and returning its reservation.
        self.release.set()
        refresh.join(1); waiting.join(1)
        self.assertFalse(refresh.is_alive()); self.assertFalse(waiting.is_alive())
        self.assertEqual(first, [("ok", {"ready": True})])
        self.assertEqual(second, [("ok", {"action": "start"})])

    def test_wait_deadline_does_not_dispatch_or_steal_refresh_ownership(self):
        self.block_refresh()
        calls = []
        self.instance.action = lambda *args: calls.append(args)
        waiting, outcome = self.background(lambda: self.host.invoke("task", "start", wait_timeout=.03))
        waiting.join(.5)
        self.assertFalse(waiting.is_alive())
        self.assertEqual(outcome[0][0], "error")
        self.assertEqual(calls, [])
        self.assertEqual(self.host.active_operations, 1)

    def test_timeout_validation_is_finite_bounded_and_does_not_dispatch(self):
        for timeout in (True, False, None, "1", -1, 31, float("inf"), float("-inf"), float("nan"), 10 ** 1000):
            with self.subTest(timeout=str(timeout)[:20]), self.assertRaises(ValueError):
                self.host.invoke("task", "start", wait_timeout=timeout)
        self.assertEqual(self.host.invoke("task", "start", wait_timeout=30), {"action": "start"})

    def test_concurrent_cancel_can_release_blocked_start(self):
        session_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        def action(name, payload):
            if name == "start":
                self.entered.set()
                if not self.release.wait(3):
                    raise RuntimeError("Task cancellation did not reach blocked start")
                return {"state": "cancelled", "session_id": session_id}
            if name == "status":
                return {"active_session_id": session_id, "session_state": "starting"}
            if name == "cancel":
                self.assertEqual(payload["session_id"], session_id)
                self.release.set()
                return {"state": "stopping"}
            raise AssertionError(name)
        self.instance.action = action
        start, outcome = self.background(lambda: self.host.invoke("task", "start"))
        self.assertTrue(self.entered.wait(1))
        self.assertEqual(self.host.invoke_concurrent("task", "status")["active_session_id"], session_id)
        self.assertEqual(self.host.invoke_concurrent("task", "cancel", {"session_id": session_id}), {"state": "stopping"})
        start.join(1)
        self.assertFalse(start.is_alive())
        self.assertEqual(outcome, [("ok", {"state": "cancelled", "session_id": session_id})])

    def test_concurrent_action_must_be_in_both_module_allowlists(self):
        for action in ("start", "observe", "arbitrary"):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "concurrent dispatch"):
                self.host.invoke_concurrent("task", action)
        self.definition.CONCURRENT_ACTIONS += ("not_exported",)
        with self.assertRaises(ValueError):
            self.host.invoke_concurrent("task", "not_exported")
        del self.definition.CONCURRENT_ACTIONS
        with self.assertRaises(ValueError):
            self.host.invoke_concurrent("task", "status")

    def test_concurrent_handler_runs_outside_lock_and_guards_lifecycle(self):
        def action(*_):
            self.entered.set()
            if not self.release.wait(3):
                raise RuntimeError("Concurrent fixture was not released")
            return {"ok": True}
        self.instance.action = action
        self.background(lambda: self.host.invoke_concurrent("task", "stop"))
        self.assertTrue(self.entered.wait(1))
        for operation in (lambda: self.host.reload("task"), self.host.dispose_all,
                          lambda: self.host.load("new"), lambda: self.host.snapshot("task")):
            thread, outcome = self.background(operation)
            thread.join(.5)
            self.assertFalse(thread.is_alive(), "Lifecycle call blocked on concurrent handler")
            self.assertEqual(outcome[0][0], "error")
            self.assertIsInstance(outcome[0][1], RuntimeError)
        self.assertEqual(self.host.active_operations, 1)

    def test_concurrent_error_and_payload_error_release_reservations(self):
        def fail(*_):
            raise OSError("Synthetic cancel failed")
        self.instance.action = fail
        with self.assertRaisesRegex(OSError, "cancel failed"):
            self.host.invoke_concurrent("task", "cancel")
        self.assertEqual(self.host.active_operations, 0)
        self.instance.action = lambda *_: {}
        with self.assertRaises(TypeError):
            self.host.invoke_concurrent("task", "status", object())
        self.assertEqual(self.host.active_operations, 0)

    def test_concurrent_callback_does_not_replace_ordinary_nested_refresh_owner(self):
        nested = threading.Event()
        cancel_running = threading.Event()
        original_owner = []
        def action(name, _payload):
            if name == "start":
                original_owner.append(threading.get_ident())
                self.entered.set()
                self.assertTrue(cancel_running.wait(1))
                self.assertEqual(self.host._operation_thread_id, original_owner[0])
                self.assertEqual(self.host.refresh("task"), {"topology": "synthetic"})
                nested.set()
                return {}
            cancel_running.set()
            self.assertTrue(nested.wait(1))
            self.assertTrue(self.release.wait(3))
            return {}
        self.instance.action = action
        ordinary, first = self.background(lambda: self.host.invoke("task", "start"))
        self.assertTrue(self.entered.wait(1))
        concurrent, second = self.background(lambda: self.host.invoke_concurrent("task", "cancel"))
        ordinary.join(1)
        self.assertFalse(ordinary.is_alive()); self.assertEqual(first, [("ok", {})])
        self.assertEqual(self.host.active_operations, 1)
        self.assertIsNone(self.host._operation_thread_id, "Finished ordinary owner must not retain nesting privilege")
        self.release.set(); concurrent.join(1)
        self.assertEqual(second, [("ok", {})])

    def test_ordinary_handler_exception_wakes_waiter_and_clears_owner(self):
        def refresh():
            self.entered.set(); self.assertTrue(self.release.wait(3))
            raise OSError("refresh failed")
        self.instance.refresh = refresh
        first, first_outcome = self.background(lambda: self.host.refresh("task"))
        self.assertTrue(self.entered.wait(1))
        second, second_outcome = self.background(lambda: self.host.invoke("task", "start", wait_timeout=1))
        self.release.set(); first.join(1); second.join(1)
        self.assertEqual(first_outcome[0][0], "error")
        self.assertEqual(second_outcome, [("ok", {"action": "start"})])

    def test_same_thread_nested_invoke_does_not_wait_for_itself(self):
        def action(*_):
            with self.assertRaisesRegex(RuntimeError, "already active"):
                self.host.invoke("task", "start", wait_timeout=30)
            return {}
        self.instance.action = action
        self.assertEqual(self.host.invoke("task", "start"), {})


if __name__ == "__main__":
    unittest.main()
