"""Journal durability: record size limits, torn tails and the verified-prefix cache."""

import hashlib
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
        self.assertEqual(self.store.control._verified[1].digest(), hashlib.sha256(data).digest())
        # A rewritten prefix invalidates the cache instead of being trusted.
        lines = data.splitlines()
        self.journal_path().write_bytes(b"\n".join([lines[1], lines[0], *lines[2:]]) + b"\n")
        with self.assertRaises(LocalError):
            self.store.control.journal(self.store.anchor)
