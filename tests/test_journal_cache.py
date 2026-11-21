"""The shared service builds a control handle per request; the verified journal must outlive it."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memoryking.adapters.local_files import Control
from memoryking.adapters.local_store import LocalStore
from memoryking.adapters.shared_control import SharedControl
from memoryking.domain.local import LocalError


class SharedJournalCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        LocalStore.initialize(base / "store", base / "control")
        self.control = base / "control"
        writer = Control(self.control)
        self.anchor = writer.read_anchor()
        for number in range(20):
            writer.append(self.anchor, {"kind": "operation", "tenant": "main", "n": number})

    def tearDown(self):
        self.temp.cleanup()

    def verified_lines(self, call):
        with mock.patch.object(
            Control, "_verify", autospec=True, side_effect=Control._verify
        ) as spy:
            call()
        return spy.call_count

    def test_a_fresh_handle_verifies_nothing_the_process_already_verified(self):
        cold = self.verified_lines(lambda: SharedControl(self.control).verified_count(self.anchor))
        self.assertEqual(cold, 20)
        again = self.verified_lines(lambda: SharedControl(self.control).verified_count(self.anchor))
        self.assertEqual(again, 0)

    def test_records_appended_by_another_process_are_verified_once(self):
        SharedControl(self.control).verified_count(self.anchor)
        other = Control(self.control)  # another process: its own handle and cache
        anchor = dict(self.anchor)
        for number in range(3):
            other.append(anchor, {"kind": "operation", "tenant": "main", "n": 100 + number})
        grown = self.verified_lines(lambda: SharedControl(self.control).verified_count(anchor))
        self.assertEqual(grown, 3)
        self.assertEqual(SharedControl(self.control).verified_count(anchor), 23)

    def test_the_anchor_still_detects_a_journal_shorter_than_it_names(self):
        shared = SharedControl(self.control)
        shared.verified_count(self.anchor)
        journal = self.control / "journal"
        lines = journal.read_bytes().splitlines()
        journal.write_bytes(b"\n".join(lines[:15]) + b"\n")
        with self.assertRaises(LocalError) as caught:
            SharedControl(self.control).verified_count(self.anchor)
        self.assertEqual(caught.exception.code, "journal_rollback")

    def test_a_replaced_journal_with_another_tail_is_verified_from_the_start(self):
        SharedControl(self.control).verified_count(self.anchor)
        journal = self.control / "journal"
        lines = journal.read_bytes().splitlines()
        # Same size is impossible to guarantee, so swap two lines near the end and append the
        # last one again: the head no longer matches, the chain breaks and verification fails.
        journal.write_bytes(b"\n".join([*lines[:18], lines[19], lines[18], lines[19]]) + b"\n")
        with self.assertRaises(LocalError):
            SharedControl(self.control).verified_count(self.anchor)

    def test_pending_records_come_from_the_file_by_sequence(self):
        shared = SharedControl(self.control)
        shared.verified_count(self.anchor)
        tail = SharedControl(self.control).records_after(self.anchor, 17)
        self.assertEqual([record["sequence"] for record in tail], [18, 19, 20])
        self.assertEqual(SharedControl(self.control).records_after(self.anchor, 20), [])
        full = SharedControl(self.control).journal(self.anchor)
        self.assertEqual(full, Control(self.control).journal(self.anchor))

    def test_warming_fills_the_cache_so_the_first_request_verifies_nothing(self):
        from memoryking.adapters.shared_service import SharedService

        tenants = Path(self.temp.name) / "tenants"
        tenants.mkdir(mode=0o700)
        (tenants / "main").symlink_to(self.control)
        (tenants / "stray").mkdir(mode=0o700)  # not a control tree: skipped, not fatal
        SharedService(None, Path(self.temp.name) / "store", tenants).warm()
        again = self.verified_lines(lambda: SharedControl(self.control).verified_count(self.anchor))
        self.assertEqual(again, 0)

    def test_the_cache_is_per_signing_key(self):
        SharedControl(self.control).verified_count(self.anchor)
        # A different tree, even at an equal size, never reuses another tree's state.
        other_base = Path(self.temp.name) / "other"
        LocalStore.initialize(other_base / "store", other_base / "control")
        other = SharedControl(other_base / "control")
        self.assertIsNot(other._cache, SharedControl(self.control)._cache)


if __name__ == "__main__":
    unittest.main()
