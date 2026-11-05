import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from modules.virtual_output_pool.module import OutputPoolModule, OutputPoolError, PROFILES
from modules.virtual_output_pool.state_store import VirtualOutputStateStore
from tests.test_virtual_output_pool import FakeBackend


class DetachedRetirementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'session.json'
        self.store = VirtualOutputStateStore(self.path)
        self.b = FakeBackend()
        self.p = OutputPoolModule(count_reader=self.b.read_count, count_writer=self.b.write_count,
            mode_reader=self.b.read_modes,mode_writer=self.b.write_mode,
            mode_restorer=self.b.restore_modes,output_window_count=self.b.window_count,
            require_empty_baseline=False)
        self.p.configure_persistence(self.store)
        self.p.begin(3,'high',tuple(PROFILES.values()),confirmed=True)
        self.original=self.store.load();self.b.count=0;self.b.modes={};self.b.writes.clear()

    def test_verified_manual_retirement_archives_before_clearing_without_display_writes(self):
        result=self.p.retire_detached(confirmed=True)
        self.assertEqual(result['state'],'off')
        self.assertFalse(self.path.exists())
        self.assertEqual(VirtualOutputStateStore(self.path.with_name(result['recovery_archive']['archive'])).load(),self.original)
        self.assertEqual(self.b.writes,[])
        self.p.dispose()

    def test_confirmation_lease_and_live_outputs_preserve_record(self):
        with self.assertRaises(OutputPoolError):self.p.retire_detached()
        self.p._session['leases']['task']=self.p._clock()
        self.assertEqual(self.p.retire_detached(confirmed=True)['reason'],'active_leases')
        self.p._session['leases'].clear();self.b.count=1
        self.assertEqual(self.p.retire_detached(confirmed=True)['state'],'cleanup_unverified')
        self.assertEqual(self.store.load(),self.original)

    def test_archive_failure_and_second_read_race_preserve_record(self):
        with patch.object(self.store,'archive_retired',side_effect=OSError('disk full')):
            self.assertEqual(self.p.retire_detached(confirmed=True)['state'],'cleanup_unverified')
        self.assertEqual(self.store.load(),self.original)
        original=self.store.archive_retired
        def race():
            result=original();self.b.count=1;return result
        with patch.object(self.store,'archive_retired',side_effect=race):
            self.assertEqual(self.p.retire_detached(confirmed=True)['state'],'cleanup_unverified')
        self.assertEqual(self.store.load(),self.original)
        self.assertIsNotNone(self.p._session)

    def test_journal_clear_failure_keeps_session_owned(self):
        with patch.object(self.store,'clear',side_effect=OSError('busy')):
            result=self.p.retire_detached(confirmed=True)
        self.assertEqual(result['reason'],'journal_clear_failed')
        self.assertIsNotNone(self.p._session)
        self.assertEqual(self.store.load(),self.original)


if __name__=='__main__':unittest.main()
