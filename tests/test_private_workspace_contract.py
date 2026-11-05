import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from modules.task_session_runtime.module import (
    OutputRequest, TargetLaunchDescriptor, TargetSpec, TaskSessionRuntime,
)
from tests.test_task_session_runtime import FakeBackend, request
from modules.task_session_runtime.isolated_target_backend import DescriptorPrivateDesktopBackend


class PrivateWorkspaceTests(unittest.TestCase):
    def target(self):
        descriptor = TargetLaunchDescriptor(
            str(Path(sys.executable).resolve()), '0' * 64, (),
            str(Path(sys.executable).resolve().parent), ('OwnedWindow',), ())
        return TargetSpec('private_qa', frozenset(), descriptor,
                          minimum_output_count=0, maximum_output_count=0,
                          output_profiles=frozenset({'private'}),
                          isolation_mode='private_desktop')

    def runtime(self, *, proof=True):
        backend = FakeBackend(proof=proof)
        lease = Mock()
        lease.readiness.side_effect = AssertionError('private workspace touched VDD readiness')
        lease.acquire.side_effect = AssertionError('private workspace acquired VDD')
        runtime = TaskSessionRuntime(backend=backend, target_registry={'private_qa': self.target()},
                                     watchdog_interval=0)
        runtime.init()
        runtime._output_lease = lease  # Poison dependency to prove this route never consumes it.
        self.addCleanup(runtime.dispose)
        return runtime, backend, lease

    def test_private_workspace_needs_no_host_outputs_or_pool(self):
        r,b,lease = self.runtime()
        status = r.refresh()
        self.assertIsNone(status['readiness_error'])
        self.assertFalse(status['output_lease_required'])
        started = r.action('start', request('start', target_app_id='private_qa', confirmed=True,
                            output_request={'output_count':0,'profile':'private'}))
        self.assertEqual(started['state'], 'ready')
        self.assertEqual(len(b.started), 1)
        stopped = r.action('stop', request('stop', session_id=started['session_id']))
        self.assertEqual(stopped['state'], 'stopped')
        lease.acquire.assert_not_called()
        lease.release.assert_not_called()

    def test_legacy_vdd_request_cannot_start_private_target(self):
        r,b,_ = self.runtime()
        result = r.action('start', request('start', target_app_id='private_qa', confirmed=True))
        self.assertEqual(result['error_code'], 'OUTPUT_REQUEST_OUTSIDE_TARGET_POLICY')
        self.assertEqual(b.started, [])

    def test_zero_request_cannot_start_legacy_target(self):
        target = TargetSpec('legacy', frozenset())
        b = FakeBackend()
        r = TaskSessionRuntime(backend=b, target_registry={'legacy':target}, watchdog_interval=0)
        r.init();self.addCleanup(r.dispose)
        result = r.action('start', request('start', target_app_id='legacy', confirmed=True,
                            output_request={'output_count':0,'profile':'private'}))
        self.assertEqual(result['error_code'], 'OUTPUT_REQUEST_OUTSIDE_TARGET_POLICY')
        self.assertEqual(b.started, [])

    def test_invalid_zero_output_combinations_are_rejected(self):
        for count, profile in [(0,'high'),(2,'private'),(False,'private'),(-1,'private'),(1,'private')]:
            with self.subTest(count=count,profile=profile), self.assertRaises(ValueError):
                OutputRequest.from_payload({'output_count':count,'profile':profile})
        for fields in ({'minimum_output_count':True},{'maximum_output_count':2},
                       {'output_profiles':frozenset({'high'})},{'launch':None},
                       {'isolation_mode':'vm'}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                replace(self.target(), **fields)

    def test_native_isolation_proof_still_required(self):
        r,b,_ = self.runtime(proof=False)
        result = r.action('start', request('start', target_app_id='private_qa', confirmed=True,
                            output_request={'output_count':0,'profile':'private'}))
        self.assertNotEqual(result['state'], 'ready')
        self.assertTrue(b.stopped)

    def test_native_cleanup_receipt_matches_real_session_schema(self):
        backend = DescriptorPrivateDesktopBackend()
        native = dict(stopped=True, target_stopped=True, job_handle_closed=True,
                      desktop_removed=True, input_desktop_unchanged=True)
        self.assertTrue(backend._stop_receipt(native).verified)
        self.assertFalse(backend._stop_receipt(dict(native, job_handle_closed=False)).verified)
        self.assertFalse(backend._stop_receipt(dict(native, job_closed=False)).verified)


if __name__ == '__main__':
    unittest.main()
