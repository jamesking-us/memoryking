"""Scheduled backups: naming, due checks, retention, the daemon hook and doctor advice."""

import tempfile
import unittest
from pathlib import Path

from test_journal import observation
from test_local_store import FakeClock

from memoryking import backups
from memoryking.adapters.local_store import LocalStore
from memoryking.domain.local import LocalError


class BackupPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        base = Path(self.temp.name)
        self.credential = LocalStore.initialize(base / "s", base / "c").read_text()
        self.clock = FakeClock()
        self.store = LocalStore(base / "s", base / "c", clock=self.clock)
        self.directory = base / "backups"

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_intervals_and_stamps(self):
        self.assertEqual(backups.interval_ms("12h"), 12 * 3_600_000)
        self.assertEqual(backups.interval_ms("1d"), 86_400_000)
        for bad in ("", "0h", "h", "5w", "-1d"):
            with self.assertRaises(LocalError):
                backups.interval_ms(bad)
        at = 1_700_000_000_000
        self.assertEqual(backups.parse_stamp("backup-" + backups.stamp(at)), at)

    def test_daemon_hook_backs_up_when_due_and_prunes_by_retention(self):
        namespace = self.store.namespace(self.credential, "demo")
        self.store.capture(self.credential, namespace, "k1", observation(1))
        self.store.backup_policy = backups.Policy(self.directory, "1h", keep=2)
        self.assertTrue(backups.due(self.directory, 3_600_000, self.clock.utc_ms()))
        self.store.housekeeping()
        first = backups.existing(self.directory)
        self.assertEqual(len(first), 1)
        self.assertTrue((first[0][1] / "manifest").is_file())
        self.assertTrue((first[0][1] / "store.sqlite").is_file())
        # Not due again within the interval, even across the once-a-minute check.
        self.clock.advance(120_000)
        self.store.housekeeping()
        self.assertEqual(len(backups.existing(self.directory)), 1)
        for _ in range(3):
            self.clock.advance(3_600_001)
            self.store.housekeeping()
        kept = backups.existing(self.directory)
        self.assertEqual(len(kept), 2, "retention keeps the newest two")
        self.assertGreater(kept[0][0], first[0][0])
        # Foreign directories in the backup root are never touched.
        (self.directory / "notes").mkdir()
        (self.directory / "backup-20200101T000000Z").mkdir()  # incomplete: no manifest
        self.clock.advance(3_600_001)
        self.store.housekeeping()
        self.assertTrue((self.directory / "notes").is_dir())
        self.assertTrue((self.directory / "backup-20200101T000000Z").is_dir())
        self.assertEqual(len(backups.existing(self.directory)), 2)

    def test_doctor_advice_for_missing_stale_and_fresh_backups(self):
        now = self.clock.utc_ms()
        self.assertEqual(backups.check(None, None, now)[0], "warn")
        self.assertEqual(backups.check(self.directory, "1d", now)[0], "warn")
        policy = backups.Policy(self.directory, "1d", keep=3)
        policy.run(self.store, now)
        self.assertEqual(backups.check(self.directory, "1d", now)[0], "ok")
        self.assertEqual(backups.check(self.directory, "1d", now + 3 * 86_400_000)[0], "fail")
        self.assertEqual(backups.check(self.directory, None, now + 30 * 86_400_000)[0], "ok")
