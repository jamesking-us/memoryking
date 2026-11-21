"""Journal durability: record size limits, torn tails and the verified-prefix cache."""

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from memoryking.adapters.local_files import JOURNAL_RECORD_LIMIT, Control
from memoryking.adapters.local_store import LocalStore
from memoryking.domain.local import LocalError


def observation(number, text="durable memory"):
    return {
        "version": 1,
        "project": "demo",
        "source": "test",
        "source_event": f"e{number}",
        "text": text,
        "readers": None,
        "classification": "internal",
        "observed_at_ms": 0,
    }


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root, self.control = base / "store", base / "control"
        self.credential = LocalStore.initialize(self.root, self.control).read_text()
        self.store = LocalStore(self.root, self.control)
        self.namespace = self.store.namespace(self.credential, "demo")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def capture(self, number):
        return self.store.capture(
            self.credential, self.namespace, f"k{number}", observation(number)
        )

    def reopen(self):
        self.store.close()
        self.store = LocalStore(self.root, self.control)
        self.namespace = self.store.namespace(self.credential, "demo")

    def journal_path(self):
        return self.control / "journal"

    def test_records_far_above_the_credential_limit_survive_reopen_and_restore(self):
        first = self.capture(1)
        backup = Path(self.temp.name) / "backup"
        self.store.backup(backup)
        bindings = [{"name": f"binding{i}", "value": "v" * 256} for i in range(32)]
        selector = {
            "selector_version": "1",
            "any_of": [{"all_of": [{"field": "path", "value": "p" * 1000}] * 16}],
        }
        action = {
            "kind": "purge",
            "tenant": "main",
            "observation": first["observation"],
            "bindings": bindings,
            "selector": selector,
        }
        # Journal through the store's own path so the size and chain rules apply.
        with self.store.transaction():
            record = self.store._journal(action)
        line = self.journal_path().read_bytes().splitlines()[-1]
        self.assertGreater(len(line), 8192)
        self.assertLess(len(line), JOURNAL_RECORD_LIMIT)
        self.assertEqual(record["sequence"], 1)
        self.assertEqual(len(Control(self.control).journal(self.store.anchor)), 1)
        self.reopen()
        self.assertTrue(self.store.healthy)
        self.assertEqual(self.store.anchor["journal_sequence"], 1)
        self.store.close()
        LocalStore.restore(self.root, self.control, backup)
        self.store = LocalStore(self.root, self.control)
        self.assertEqual(self.store.anchor["journal_sequence"], 1)

    def test_oversized_record_is_refused_before_writing_and_store_stays_healthy(self):
        first = self.capture(1)
        before = self.journal_path().read_bytes()
        anchor = dict(self.store.anchor)
        action = {
            "kind": "purge",
            "tenant": "main",
            "observation": first["observation"],
            "payload": "x" * JOURNAL_RECORD_LIMIT,
        }
        with self.assertRaises(LocalError) as caught:
            with self.store.transaction():
                self.store._journal(action)
        self.assertEqual(caught.exception.code, "journal_record_limit")
        self.assertEqual(before, self.journal_path().read_bytes())
        self.assertEqual(anchor, self.store.anchor)
        self.assertTrue(self.store.healthy)
        self.capture(2)
        self.reopen()
        self.assertEqual(self.store.anchor["journal_sequence"], 0)

    def test_unanchored_torn_tail_is_discarded_and_truncated_on_next_append(self):
        first = self.capture(1)
        self.store.purge(self.credential, first["observation"])
        self.assertEqual(self.store.anchor["journal_sequence"], 1)
        intact = self.journal_path().read_bytes()
        with self.journal_path().open("ab") as stream:
            stream.write(b"eyJzZXF1ZW5jZSI6Mi")  # a write cut off by a crash: no newline
        self.reopen()
        self.assertTrue(self.store.healthy)
        self.assertEqual(self.store.anchor["journal_sequence"], 1)
        second = self.capture(2)
        self.store.purge(self.credential, second["observation"])
        data = self.journal_path().read_bytes()
        self.assertTrue(data.startswith(intact))
        self.assertNotIn(b"eyJzZXF1ZW5jZSI6Mi\n", data)
        self.assertEqual(len(data.splitlines()), 2)
        self.assertEqual(self.store.anchor["journal_sequence"], 2)

    def test_database_ahead_of_the_journal_fails_closed_at_open(self):
        first = self.capture(1)
        stale = {name: (self.control / name).read_bytes() for name in ("journal", "anchor")}
        self.store.purge(self.credential, first["observation"])
        self.assertEqual(self.store.anchor["journal_sequence"], 1)
        self.store.close()
        # A journal tail lost after the database commit (and its anchor still behind), or a
        # control tree copied from before the action, leaves the database ahead of the journal.
        for name, data in stale.items():
            (self.control / name).write_bytes(data)
        with self.assertRaises(LocalError) as caught:
            LocalStore(self.root, self.control)
        self.assertEqual(caught.exception.code, "journal_rollback")

    def test_damage_inside_the_anchored_journal_stays_fatal(self):
        first = self.capture(1)
        self.store.purge(self.credential, first["observation"])
        second = self.capture(2)
        self.store.purge(self.credential, second["observation"])
        lines = self.journal_path().read_bytes().splitlines()
        self.store.close()
        self.journal_path().write_bytes(lines[0][:-10] + b"\n" + lines[1] + b"\n")
        fatal = {"invalid_token", "journal_invalid", "journal_rollback", "recovery_required"}
        with self.assertRaises(LocalError) as caught:
            LocalStore(self.root, self.control)
        self.assertIn(caught.exception.code, fatal)
        # A truncated anchored final line is rollback, not a torn tail.
        self.journal_path().write_bytes(lines[0] + b"\n" + lines[1][:-10])
        with self.assertRaises(LocalError) as caught:
            LocalStore(self.root, self.control)
        self.assertIn(caught.exception.code, fatal)
        self.journal_path().write_bytes(b"\n".join(lines) + b"\n")
        self.store = LocalStore(self.root, self.control)

    def test_restore_issues_the_credential_only_to_a_currently_valid_admin_grant(self):
        # Sorted first, but neither holds admin rights at restore time.
        with self.store.transaction():
            for row in (
                ("main", "aaa-reader", "*", '["read"]', None, None),
                ("main", "aab-expired", "*", '["admin"]', None, 1),
            ):
                self.store.db.execute("INSERT INTO grants VALUES (?,?,?,?,?,?)", row)
        backup = Path(self.temp.name) / "backup"
        self.store.backup(backup)
        self.store.close()
        token = LocalStore.restore(self.root, self.control, backup).read_text()
        identity = Control(self.control).verify(token)
        self.assertEqual(identity["principal"], "owner")
        self.store = LocalStore(self.root, self.control)

    def test_verified_prefix_cache_matches_a_full_reread(self):
        for number in range(1, 6):
            result = self.capture(number)
            self.store.purge(self.credential, result["observation"])
        cached = self.store.control.journal(self.store.anchor)
        fresh = Control(self.control).journal(self.store.anchor)
        self.assertEqual(cached, fresh)
        self.assertEqual([r["sequence"] for r in fresh], [1, 2, 3, 4, 5])
        data = self.journal_path().read_bytes()
        state = self.store.control._cache.state
        self.assertEqual(state.size, len(data))
        self.assertEqual(
            bytes(state.hashes),
            b"".join(hashlib.sha256(line).digest() for line in data.splitlines()),
        )
        # A rewritten file invalidates the cache instead of being trusted. The rewrite keeps
        # the size and the last line, so only the changed modification time shows it; the
        # time is set explicitly because a fast test can fall inside one clock tick.
        lines = data.splitlines()
        self.journal_path().write_bytes(b"\n".join([lines[1], lines[0], *lines[2:]]) + b"\n")
        stat = self.journal_path().stat()
        os.utime(self.journal_path(), ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
        with self.assertRaises(LocalError):
            self.store.control.journal(self.store.anchor)

    def journal_actions(self, count):
        """Policy records through the store's own path, to build a journal of a given length."""
        for _ in range(count):
            self.serial = getattr(self, "serial", 1000) + 1
            first = self.capture(self.serial)
            self.store.purge(self.credential, first["observation"])

    def test_extending_the_cache_checks_only_the_tail_and_keeps_every_other_check(self):
        self.journal_actions(3)
        control = self.store.control
        anchor = dict(self.store.anchor)
        self.assertEqual(control.verified_count(anchor), 3)
        # A second handle on the same tree, as another process would be: it appends, and this
        # one extends its cache from the old position rather than trusting a stale count.
        other = Control(self.control)
        other_anchor = dict(anchor)
        other.append(other_anchor, {"kind": "grant", "tenant": "main"})
        self.assertEqual(control.verified_count(other_anchor), 4)
        # The older anchor still matches its own sequence: the longer journal is not a rollback.
        self.assertEqual(control.verified_count(anchor), 4)
        # An anchor naming a head the journal does not have at that sequence is refused.
        wrong = {**other_anchor, "journal_head": "1" * 64}
        with self.assertRaises(LocalError) as caught:
            control.verified_count(wrong)
        self.assertEqual(caught.exception.code, "journal_rollback")

    def test_truncation_and_rewrite_are_never_served_from_the_cache(self):
        self.journal_actions(3)
        control = self.store.control
        anchor = dict(self.store.anchor)
        control.journal(anchor)
        lines = self.journal_path().read_bytes().splitlines()
        # Shorter than the cache and the anchor: rollback, not a cache hit.
        self.journal_path().write_bytes(b"\n".join(lines[:2]) + b"\n")
        with self.assertRaises(LocalError) as caught:
            control.verified_count(anchor)
        self.assertEqual(caught.exception.code, "journal_rollback")
        # Truncated and rewritten with a different (longer) tail: the last verified line no
        # longer matches the cached head, so everything is verified again and the damage found.
        self.journal_path().write_bytes(
            b"\n".join([lines[0], lines[2], lines[1], lines[2]]) + b"\n"
        )
        with self.assertRaises(LocalError):
            control.verified_count(anchor)
        self.journal_path().write_bytes(b"\n".join(lines) + b"\n")
        self.assertEqual(control.verified_count(anchor), 3)

    def test_a_rewrite_that_keeps_size_head_and_time_cannot_change_what_was_verified(self):
        self.journal_actions(3)
        control = self.store.control
        anchor = dict(self.store.anchor)
        before = control.journal(anchor)
        data = self.journal_path().read_bytes()
        stat = self.journal_path().stat()
        lines = data.splitlines()
        self.journal_path().write_bytes(b"\n".join([lines[1], lines[0], lines[2]]) + b"\n")
        os.utime(self.journal_path(), ns=(stat.st_atime_ns, stat.st_mtime_ns))
        # The cache serves only records it verified, never the rewritten bytes...
        self.assertEqual(control.journal(anchor), before)
        # ...and a cold verification, which is what a restart does, refuses the damage.
        with self.assertRaises(LocalError):
            Control(self.control).journal(anchor)

    def test_append_after_cache_use_matches_a_cold_reader(self):
        self.journal_actions(2)
        self.assertEqual(
            self.store.control.journal(self.store.anchor),
            Control(self.control).journal(self.store.anchor),
        )
        self.journal_actions(1)
        cold = Control(self.control)
        self.assertEqual(len(cold.journal(self.store.anchor)), 3)
        self.assertEqual(
            self.store.control.journal(self.store.anchor), cold.journal(self.store.anchor)
        )
