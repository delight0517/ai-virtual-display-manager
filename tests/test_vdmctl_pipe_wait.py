import ctypes
import json
import unittest
from unittest.mock import patch

from tools.vdmctl import (CONTROL_PIPE_WAIT_TIMEOUT_MS, OPERATOR_PIPE_WAIT_TIMEOUT_MS,
                          pipe_wait_timeout_ms, send_request)


class FakeFunction:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class FakeKernel32:
    def __init__(self):
        self.wait_calls = 0
        self.response = b""
        self.WaitNamedPipeW = FakeFunction(self.wait_named_pipe)
        self.CreateFileW = FakeFunction(lambda *_args: 17)
        self.WriteFile = FakeFunction(self.write_file)
        self.ReadFile = FakeFunction(self.read_file)
        self.CloseHandle = FakeFunction(lambda *_args: 1)

    def wait_named_pipe(self, _path, _timeout):
        self.wait_calls += 1
        return int(self.wait_calls >= 4)

    def write_file(self, _handle, request_buffer, size, written, _overlapped):
        request = json.loads(request_buffer.raw[:size].decode("utf-8"))
        self.response = (json.dumps({"protocol_version": 1,
                                     "request_id": request["request_id"],
                                     "ok": True, "data": {"state": "ok"}})
                         + "\n").encode("utf-8")
        written._obj.value = size
        return 1

    def read_file(self, _handle, buffer, size, read, _overlapped):
        ctypes.memmove(buffer, self.response, min(size, len(self.response)))
        read._obj.value = len(self.response)
        self.response = b""
        return 1


class VdmctlPipeWaitTests(unittest.TestCase):
    def test_operator_queries_wait_longer_than_isolated_control_pipes(self):
        self.assertEqual(pipe_wait_timeout_ms("status"), OPERATOR_PIPE_WAIT_TIMEOUT_MS)
        self.assertEqual(pipe_wait_timeout_ms("screens"), OPERATOR_PIPE_WAIT_TIMEOUT_MS)
        self.assertEqual(pipe_wait_timeout_ms("ui.status"), CONTROL_PIPE_WAIT_TIMEOUT_MS)
        self.assertEqual(pipe_wait_timeout_ms("task.status"), CONTROL_PIPE_WAIT_TIMEOUT_MS)
        self.assertGreater(OPERATOR_PIPE_WAIT_TIMEOUT_MS, CONTROL_PIPE_WAIT_TIMEOUT_MS)

    def test_retries_transient_busy_pipe_until_listener_is_available(self):
        api = FakeKernel32()
        with patch("tools.vdmctl.ctypes.get_last_error", return_value=121), \
                patch("tools.vdmctl.time.sleep"):
            result = send_request("status", {}, timeout_ms=OPERATOR_PIPE_WAIT_TIMEOUT_MS,
                                  api=api, pipe_path=r"\\.\pipe\VDM-Test")
        self.assertTrue(result["ok"])
        self.assertEqual(api.wait_calls, 4)


if __name__ == "__main__":
    unittest.main()
