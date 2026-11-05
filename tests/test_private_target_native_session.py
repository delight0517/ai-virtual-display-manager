from __future__ import annotations

import ctypes
import hashlib
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.png_capture import encode_bgra_png
from modules.private_desktop.module import (
    _ExtendedLimitInformation, _ProcessInformation, _StartupInfo,
)
from modules.private_desktop.session import (
    DiagnosticSession, DiagnosticSessionStartError,
)
from modules.task_session_runtime.module import TargetLaunchDescriptor


PNG = encode_bgra_png(bytes((0, 0, 0, 0, 0, 0, 255, 0,
                             0, 255, 0, 0, 255, 0, 0, 0)), 2, 2)


class FakePrivateDesktopWin32:
    CALLBACK = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                                 ctypes.c_ssize_t)

    def __init__(self, executable_path, *, assign=True, change_image_after_create=False):
        from ctypes import wintypes
        self.executable_path = str(Path(executable_path).resolve())
        self.assign = assign
        self.change_image_after_create = change_image_after_create
        self.input_names = ["Default"]
        self.window_live = False
        self.process_stopped = False
        self.close_requested = False
        self.process_assigned = False
        self.resume_calls = 0
        self.terminate_process_calls = []
        self.terminate_job_calls = []
        self.input_thread_ids = []
        self.input_window_thread_ids = []
        self.launch = None
        self.desktop_closed = False
        self.user32 = types.SimpleNamespace()
        self.kernel32 = types.SimpleNamespace()
        u, k = self.user32, self.kernel32
        u.CreateDesktopW = self.create_desktop
        u.CloseDesktop = self.close_desktop
        u.GetThreadDesktop = lambda _tid: 77
        u.GetWindowThreadProcessId = self.window_pid
        u.GetWindowTextW = lambda _hwnd, buffer, _size: self.set_buffer(buffer, "Target")
        u.GetClassNameW = lambda _hwnd, buffer, _size: self.set_buffer(
            buffer, "PinnedMainWindow")
        u.EnumDesktopWindows = self.enum_windows
        u.PostMessageW = self.post_message
        k.GetCurrentThreadId = lambda: 1
        k.CreateJobObjectW = lambda *_args: 88
        k.SetInformationJobObject = lambda *_args: True
        k.CreateProcessW = self.create_process
        k.GetProcessId = lambda _handle: 777
        k.AssignProcessToJobObject = self.assign_process
        k.ResumeThread = self.resume_thread
        k.OpenProcess = lambda *_args: 99
        k.IsProcessInJob = self.is_process_in_job
        k.QueryFullProcessImageNameW = self.query_image
        k.CloseHandle = lambda _handle: True
        k.WaitForSingleObject = self.wait_process
        k.TerminateProcess = self.terminate_process
        k.TerminateJobObject = self.terminate_job
        k.GetExitCodeProcess = self.exit_code

    @staticmethod
    def set_buffer(buffer, value):
        buffer.value = value
        return len(value)

    def create_desktop(self, name, *_args):
        self.desktop_name = name
        return 55

    def close_desktop(self, _handle):
        self.desktop_closed = True
        return True

    def enum_windows(self, _desktop, callback, param):
        if self.window_live:
            callback(123, param)
        return True

    def window_pid(self, _hwnd, pid_ptr):
        from ctypes import wintypes
        ctypes.cast(pid_ptr, ctypes.POINTER(wintypes.DWORD))[0] = 777
        return 2

    def post_message(self, _hwnd, *_args):
        self.close_requested = True
        return True

    def create_process(self, executable, command, _pa, _ta, _inherit, _flags,
                       _env, cwd, startup, process_info):
        self.launch = {"executable": executable, "command": command.value,
                       "cwd": cwd,
                       "desktop": ctypes.cast(
                           startup, ctypes.POINTER(_StartupInfo)).contents.lpDesktop}
        info = ctypes.cast(process_info, ctypes.POINTER(_ProcessInformation)).contents
        info.hProcess, info.hThread = 100, 101
        info.dwProcessId, info.dwThreadId = 777, 2
        if self.change_image_after_create:
            Path(self.executable_path).write_bytes(b"replaced during suspended launch")
        return True

    def assign_process(self, _job, _process):
        self.process_assigned = self.assign
        return self.assign

    def resume_thread(self, _thread):
        self.resume_calls += 1
        self.window_live = True
        return 1

    def is_process_in_job(self, _process, _job, in_job_ptr):
        from ctypes import wintypes
        ctypes.cast(in_job_ptr, ctypes.POINTER(wintypes.BOOL))[0] = self.process_assigned
        return True

    def query_image(self, _process, _flags, image, length_ptr):
        from ctypes import wintypes
        image.value = self.executable_path
        ctypes.cast(length_ptr, ctypes.POINTER(wintypes.DWORD))[0] = len(image.value)
        return True

    def wait_process(self, _handle, _timeout):
        if self.close_requested:
            self.process_stopped = True
            self.window_live = False
        return 0 if self.process_stopped else 258

    def terminate_process(self, handle, _code):
        self.terminate_process_calls.append(handle)
        self.process_stopped = True
        return True

    def terminate_job(self, job, _code):
        self.terminate_job_calls.append(job)
        self.process_stopped = True
        self.window_live = False
        return True

    def exit_code(self, _handle, code_ptr):
        from ctypes import wintypes
        ctypes.cast(code_ptr, ctypes.POINTER(wintypes.DWORD))[0] = 0
        return True

    def input_desktop_name(self, manager_thread_id=None):
        self.input_thread_ids.append(manager_thread_id)
        return self.input_names.pop(0) if len(self.input_names) > 1 else self.input_names[0]

    def window_is_on_input_desktop(self, _hwnd, manager_thread_id=None):
        self.input_window_thread_ids.append(manager_thread_id)
        return False

    def object_name(self, handle):
        return "Default" if handle == 77 else self.desktop_name

    def desktop_exists(self, _name):
        return not self.desktop_closed

    @staticmethod
    def target_command(descriptor):
        return str(Path(descriptor.executable_path).resolve()), list(descriptor.arguments)

    @staticmethod
    def capture_png(_hwnd):
        return PNG, 2, 2, 4


class NativeDescriptorSessionTests(unittest.TestCase):
    def make_descriptor(self, folder, path):
        path.write_bytes(b"pinned test image")
        return TargetLaunchDescriptor(
            executable_path=str(path.resolve()),
            executable_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            arguments=("--isolated-test",), working_directory=str(folder.resolve()),
            window_class_names=("PinnedMainWindow",), actions=(),
            maximum_session_seconds=60)

    def test_mocked_native_session_launches_observes_and_cleans_exact_private_target(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            executable = directory / "target.exe"
            descriptor = self.make_descriptor(directory, executable)
            api = FakePrivateDesktopWin32(executable)
            session = DiagnosticSession(api, _StartupInfo, _ProcessInformation,
                                        _ExtendedLimitInformation,
                                        target_descriptor=descriptor,
                                        session_id="native-session-test")
            identity = session.identity()
            self.assertEqual(identity["executable_sha256"], descriptor.executable_sha256)
            self.assertTrue(identity["window_absent_from_input_desktop"])
            self.assertTrue(api.process_assigned)
            self.assertEqual(set(api.input_thread_ids), {1})
            self.assertEqual(api.input_window_thread_ids, [1])
            self.assertEqual(api.launch["executable"], descriptor.executable_path)
            self.assertEqual(api.launch["cwd"], descriptor.working_directory)
            self.assertTrue(api.launch["desktop"].startswith("WinSta0\\AIVDDS_"))
            observed = session.observe(timeout_seconds=5)
            self.assertEqual(observed["png_bytes"], PNG)
            self.assertTrue(observed["content_verified"])
            receipt = session.stop(timeout_seconds=5)
            self.assertTrue(receipt["stopped"])
            self.assertTrue(receipt["input_desktop_unchanged"])
            self.assertTrue(api.desktop_closed)

    def test_capture_diagnostic_code_survives_private_session_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            executable = directory / "target.exe"
            descriptor = self.make_descriptor(directory, executable)
            api = FakePrivateDesktopWin32(executable)

            def failed_capture(_hwnd):
                error = RuntimeError("private detail must not cross the boundary")
                error.diagnostic_code = "capture_print_window_failed"
                raise error

            api.capture_png = failed_capture
            session = DiagnosticSession(api, _StartupInfo, _ProcessInformation,
                                        _ExtendedLimitInformation,
                                        target_descriptor=descriptor,
                                        session_id="native-capture-diagnostic")
            observed = session.observe(timeout_seconds=5)
            self.assertEqual(observed, {
                "error_code": "capture_print_window_failed"})
            receipt = session.stop(timeout_seconds=5)
            self.assertTrue(receipt["stopped"])
            self.assertTrue(api.desktop_closed)

    def test_failed_job_assignment_terminates_exact_suspended_process(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            executable = directory / "target.exe"
            descriptor = self.make_descriptor(directory, executable)
            api = FakePrivateDesktopWin32(executable, assign=False)
            with self.assertRaises(DiagnosticSessionStartError):
                DiagnosticSession(api, _StartupInfo, _ProcessInformation,
                                  _ExtendedLimitInformation,
                                  target_descriptor=descriptor,
                                  session_id="native-start-failure")
            self.assertEqual(api.terminate_process_calls, [100])
            self.assertEqual(api.terminate_job_calls, [])
            self.assertEqual(api.resume_calls, 0)
            self.assertTrue(api.desktop_closed)

    def test_image_swap_while_suspended_is_terminated_inside_owned_job(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            executable = directory / "target.exe"
            descriptor = self.make_descriptor(directory, executable)
            api = FakePrivateDesktopWin32(executable, change_image_after_create=True)
            with self.assertRaises(DiagnosticSessionStartError):
                DiagnosticSession(api, _StartupInfo, _ProcessInformation,
                                  _ExtendedLimitInformation,
                                  target_descriptor=descriptor,
                                  session_id="native-hash-failure")
            self.assertTrue(api.process_assigned)
            self.assertEqual(api.resume_calls, 0)
            self.assertEqual(api.terminate_job_calls, [88])
            self.assertEqual(api.terminate_process_calls, [])
            self.assertTrue(api.desktop_closed)

    def test_timed_out_observation_stops_session_after_inflight_capture_returns(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            executable = directory / "target.exe"
            descriptor = self.make_descriptor(directory, executable)
            api = FakePrivateDesktopWin32(executable)
            entered_capture, finish_capture = threading.Event(), threading.Event()

            def blocked_capture(_hwnd):
                entered_capture.set()
                finish_capture.wait(2)
                return PNG, 2, 2, 4

            api.capture_png = blocked_capture
            session = DiagnosticSession(api, _StartupInfo, _ProcessInformation,
                                        _ExtendedLimitInformation,
                                        target_descriptor=descriptor,
                                        session_id="native-timeout-stop")
            timeout_bound = DiagnosticSession.ACTION_TIMEOUT_SECONDS
            DiagnosticSession.ACTION_TIMEOUT_SECONDS = 0.05
            try:
                with self.assertRaises(TimeoutError):
                    session.observe(timeout_seconds=5)
                self.assertTrue(entered_capture.wait(1))
                self.assertTrue(session._stop_requested)
                with self.assertRaisesRegex(RuntimeError, "stopping"):
                    session.observe(timeout_seconds=5)
            finally:
                DiagnosticSession.ACTION_TIMEOUT_SECONDS = timeout_bound
                finish_capture.set()

            receipt = session.close()
            self.assertTrue(receipt["stopped"])
            self.assertTrue(receipt["target_stopped"])
            self.assertTrue(receipt["desktop_removed"])
            self.assertTrue(api.desktop_closed)


if __name__ == "__main__":
    unittest.main()
