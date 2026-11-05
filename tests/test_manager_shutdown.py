import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from queue import SimpleQueue
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import ManagerApp


class ManagerShutdownTests(unittest.TestCase):
    def make_app(self):
        app = ManagerApp.__new__(ManagerApp)
        app._busy = False
        app._closing = False
        app._shutdown_in_progress = False
        app._shutdown_deadline = None
        app._completed = SimpleQueue()
        app._executor = ThreadPoolExecutor(max_workers=1)
        app.root = Mock()
        app.status_title = Mock()
        app.status_detail = Mock()
        app.protection_detail = Mock()
        for name in ("refresh_button", "enable_button", "disable_button", "start_button",
                     "stop_button", "reload_button", "router_button", "private_probe_button",
                     "physical_guard_button", "single_output_button", "startup_button"):
            button = Mock()
            button.cget.return_value = "normal"
            setattr(app, name, button)

        cli = Mock()
        cli.pause_for_shutdown.return_value = True
        guard = Mock()
        guard.status.return_value = {"enabled": True}
        router = SimpleNamespace(guard=guard, action=Mock())
        diagnostics = SimpleNamespace(instance=Mock())
        app.host = Mock()
        app.host.active_operations = 0
        app.host.invoke.return_value = {"state": "off"}
        app.host._loaded = {
            "manager_cli": SimpleNamespace(instance=cli),
            "app_window_router": SimpleNamespace(instance=router),
            "display_diagnostics": diagnostics,
        }
        app._task_runtime_action = Mock(return_value={"active_session_id": None})
        app._shutdown_notice = Mock()
        return app, cli, guard

    def finish_next(self, app):
        completion = app._completed.get(timeout=3)
        future, done = completion[:2]
        quiet = bool(completion[2]) if len(completion) > 2 else False
        app._finish(future, done, quiet=quiet)

    def tearDown(self):
        # Each test registers its executor on self so a failed assertion cannot
        # leave a background worker running.
        executor = getattr(self, "executor", None)
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)

    @patch("app.get_keep_awake_guard")
    def test_guard_drain_retries_in_worker_and_then_closes(self, awake):
        app, cli, guard = self.make_app()
        self.executor = app._executor
        guard.stop.side_effect = [{"enabled": True}, {"enabled": False}]
        worker_threads = []
        cli.pause_for_shutdown.side_effect = lambda: worker_threads.append(threading.get_ident()) or True
        main_thread = threading.get_ident()

        app.close()
        self.finish_next(app)
        self.assertFalse(app._closing)
        app.root.after.assert_called_once_with(250, app.close)
        app.root.destroy.assert_not_called()
        self.assertTrue(worker_threads)
        self.assertNotEqual(worker_threads[0], main_thread)

        app.close()
        self.finish_next(app)
        self.assertTrue(app._closing)
        app.host.dispose_all.assert_called_once()
        app.root.destroy.assert_called_once()
        cli.resume_after_shutdown.assert_not_called()
        awake.return_value.stop.assert_called_once()

    @patch("app.get_keep_awake_guard")
    def test_active_read_only_cli_request_drains_without_aborting_shutdown(self, awake):
        app, cli, guard = self.make_app()
        self.executor = app._executor
        cli.pause_for_shutdown.side_effect = [False, True]
        guard.stop.return_value = {"enabled": False}

        app.close()
        self.finish_next(app)
        self.assertFalse(app._closing)
        self.assertTrue(app._shutdown_in_progress)
        app.root.after.assert_called_once_with(250, app.close)
        app._shutdown_notice.assert_not_called()
        app.host.dispose_all.assert_not_called()

        app.close()
        self.finish_next(app)
        self.assertTrue(app._closing)
        app.host.dispose_all.assert_called_once()
        app.root.destroy.assert_called_once()
        awake.return_value.stop.assert_called_once()

    def test_pool_failure_reopens_cli_and_keeps_manager_running(self):
        app, cli, guard = self.make_app()
        self.executor = app._executor
        app.host.invoke.side_effect = RuntimeError("pool status unavailable")
        app.close()
        self.finish_next(app)
        cli.resume_after_shutdown.assert_called_once()
        guard.stop.assert_not_called()
        app.host.dispose_all.assert_not_called()
        app.root.destroy.assert_not_called()
        app._shutdown_notice.assert_called_once()
        self.assertFalse(app._shutdown_in_progress)

    def test_cli_stop_checks_task_state_off_tk_thread(self):
        app, cli, guard = self.make_app()
        self.executor = app._executor
        worker_threads = []
        app._task_runtime_action.side_effect = lambda *_: (
            worker_threads.append(threading.get_ident()) or {"active_session_id": None})
        main_thread = threading.get_ident()
        app.close(verify_no_active_task=True)
        self.finish_next(app)
        self.assertTrue(worker_threads)
        self.assertNotEqual(worker_threads[0], main_thread)
        cli.pause_for_shutdown.assert_called_once()

    def test_active_task_refuses_shutdown_without_stopping_cli(self):
        app, cli, guard = self.make_app()
        self.executor = app._executor
        app._task_runtime_action.return_value = {"active_session_id": "active"}
        app.close(verify_no_active_task=True)
        self.finish_next(app)
        cli.pause_for_shutdown.assert_not_called()
        guard.stop.assert_not_called()
        app.host.dispose_all.assert_not_called()
        app._shutdown_notice.assert_called_once()

    def test_protection_probe_runs_outside_tk_and_does_not_block_callback(self):
        app, _, _ = self.make_app()
        self.executor = app._executor
        started = threading.Event()
        release = threading.Event()
        worker_threads = []
        app.host._loaded["app_window_router"].instance.guard = None
        app.host.info.return_value = {"version": "test"}
        def read_guard(*_):
            worker_threads.append(threading.get_ident())
            started.set()
            release.wait(2)
            return {"runtime": {"state": "off", "moved_count": 0}}
        app.host.invoke.side_effect = read_guard
        app._ensure_default_guard = Mock(return_value=None)
        resident = {"developer_paused": False, "startup_enabled": True}
        with patch("app.resident_controller") as controller, patch("app.atomic_json"):
            controller.return_value.status.return_value = resident
            t0 = time.perf_counter()
            app._protection_tick()
            elapsed = time.perf_counter() - t0
            self.assertLess(elapsed, 0.1)
            self.assertTrue(started.wait(1))
            self.assertNotEqual(worker_threads[0], threading.get_ident())
            release.set()
            self.finish_next(app)
        app.protection_detail.configure.assert_called_once()

    def test_pool_reaping_and_status_run_outside_tk(self):
        app, _, _ = self.make_app()
        self.executor = app._executor
        started = threading.Event()
        release = threading.Event()
        worker_threads = []
        task_lease = Mock()
        output_lease = Mock()
        task_lease.reap_expired.side_effect = lambda: worker_threads.append(threading.get_ident())
        output_lease.reap_expired.side_effect = lambda: worker_threads.append(threading.get_ident())
        app.host._loaded.update({
            "task_display_lease": SimpleNamespace(instance=task_lease),
            "only_chat_output_lease": SimpleNamespace(instance=output_lease),
        })
        def read_pool(*_):
            worker_threads.append(threading.get_ident())
            started.set()
            release.wait(2)
            return {"state": "off"}
        app.host.invoke.side_effect = read_pool

        app._pool_tick()
        self.assertTrue(started.wait(1))
        self.assertTrue(all(thread != threading.get_ident() for thread in worker_threads))
        release.set()
        self.finish_next(app)
        task_lease.reap_expired.assert_called_once()
        output_lease.reap_expired.assert_called_once()


if __name__ == "__main__":
    unittest.main()
