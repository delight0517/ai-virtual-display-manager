import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from core.vdd_pipe import send

class PipeTransportTests(unittest.TestCase):
    def kernel(self, partial=False):
        def write(handle, payload, size, written, overlapped):
            written._obj.value = size - 1 if partial else size
            return 1
        return SimpleNamespace(CreateFileW=Mock(return_value=123),
            SetNamedPipeHandleState=Mock(return_value=1), WriteFile=Mock(side_effect=write),
            ReadFile=Mock(return_value=0), CloseHandle=Mock(return_value=1))

    def test_disconnect_after_full_write_means_delivered_without_reply(self):
        k = self.kernel()
        with patch('core.vdd_pipe.ctypes.WinDLL', return_value=k), patch('core.vdd_pipe.ctypes.get_last_error', return_value=233):
            self.assertEqual(send('PING'), 'DELIVERED_NO_REPLY')
        k.CloseHandle.assert_called_once_with(123)

    def test_partial_write_is_never_claimed_as_delivered(self):
        k = self.kernel(partial=True)
        with patch('core.vdd_pipe.ctypes.WinDLL', return_value=k):
            with self.assertRaisesRegex(OSError, 'Incomplete'):
                send('PING')
        k.ReadFile.assert_not_called()
        k.CloseHandle.assert_called_once_with(123)

    def test_unexpected_read_failure_is_not_hidden_as_delivery(self):
        k = self.kernel()
        with patch('core.vdd_pipe.ctypes.WinDLL', return_value=k), patch('core.vdd_pipe.ctypes.get_last_error', return_value=5):
            with self.assertRaises(OSError):
                send('PING')
        k.CloseHandle.assert_called_once_with(123)
