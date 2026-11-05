"""Owner routing uses synthetic backends; no desktop/app is launched."""
import tempfile
import unittest
from pathlib import Path
from test_task_session_runtime import (
    FakeBackend, request, TargetLaunchDescriptor, TargetSpec, TaskSessionRuntime,
)


class OwnerSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        descriptor = TargetLaunchDescriptor(
            str(self.root / 'synthetic.exe'), '0' * 64,
            ('--profile={VDM_TASK_PROFILE}',), str(self.root), ('Synthetic',), ())
        self.target = TargetSpec('synthetic', frozenset(), descriptor, 0, 0,
                                 frozenset({'private'}), 'private_desktop')
        self.backend = FakeBackend()
        self.runtime = TaskSessionRuntime(self.backend, {'synthetic': self.target}, watchdog_interval=0)
        self.runtime.init()

    def tearDown(self):
        self.runtime.dispose()
        self.temp.cleanup()

    def start(self, owner):
        return self.runtime.handle_request(request(
            'start', owner_id=owner, target_app_id='synthetic', confirmed=True,
            output_request={'output_count': 0, 'profile': 'private'}))

    def test_two_owners_have_independent_sessions_and_cleanup(self):
        a, b = self.start('chat-a'), self.start('chat-b')
        self.assertIsNone(a['error_code'])
        self.assertIsNone(b['error_code'])
        self.assertNotEqual(a['session_id'], b['session_id'])
        # The manager's global shutdown gate must also see owner sessions.
        self.assertIsNotNone(self.runtime.handle_request(request('status'))['active_session_id'])
        denied = self.runtime.handle_request(request('stop', owner_id='chat-b', session_id=a['session_id']))
        self.assertEqual('SESSION_NOT_FOUND', denied['error_code'])
        stopped = self.runtime.handle_request(request('stop', owner_id='chat-a', session_id=a['session_id']))
        self.assertEqual('stopped', stopped['state'])
        self.assertEqual(1, len(self.backend.stopped))
        still_running = self.runtime.handle_request(request('status', owner_id='chat-b'))
        self.assertEqual(b['session_id'], still_running['active_session_id'])
        with self.assertRaises(RuntimeError):
            self.runtime.snapshot()

    def test_shared_profile_target_is_rejected(self):
        from dataclasses import replace
        self.runtime._targets['synthetic'] = replace(
            self.target, launch=replace(self.target.launch, arguments=()))
        self.assertEqual('ISOLATED_APP_PROFILE_REQUIRED', self.start('chat-a')['error_code'])
        self.assertFalse(self.backend.started)

    def test_replay_ledger_survives_owner_snapshot(self):
        start = request('start', owner_id='chat-a', target_app_id='synthetic', confirmed=True,
                        output_request={'output_count': 0, 'profile': 'private'})
        result = self.runtime.handle_request(start)
        self.runtime.handle_request(request('stop', owner_id='chat-a', session_id=result['session_id']))
        snapshot = self.runtime.snapshot()
        restored = TaskSessionRuntime(self.backend, {'synthetic': self.target}, watchdog_interval=0)
        restored.restore(snapshot)
        restored.init()
        try:
            self.assertEqual('REQUEST_REPLAYED', restored.handle_request(start)['error_code'])
        finally:
            restored.dispose()
