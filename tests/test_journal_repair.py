"""A journal record that cannot be applied is listed, never skipped, and never fences for good."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_journal import observation
from test_local_store import FakeClock

from memoryking.adapters.local_files import APPLY_FAILURE, Control
from memoryking.adapters.local_store import LocalStore
from memoryking.domain.local import LocalError

SENTINEL = "sentinel-principal-name"


def grant(tenant="main", principal=SENTINEL):
    return {
        "kind": "grant",
        "tenant": tenant,
        "actor": "operator",
        "principal": principal,
        "project": "demo",
        "capabilities": ["read"],
        "starts": None,
        "expires": None,
    }


class LocalJournalRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root, self.control = base / "store", base / "control"
        self.credential = LocalStore.initialize(self.root, self.control).read_text()
        self.clock = FakeClock()
        self.store = LocalStore(self.root, self.control, clock=self.clock)
        self.namespace = self.store.namespace(self.credential, "demo")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def append_while_closed(self, *actions):
        self.store.close()
        control = Control(self.control)
        anchor = control.read_anchor()
        for action in actions:
            control.append(anchor, action)

    def test_purge_that_overruns_its_budget_is_applied_without_a_restart(self):
        first = self.store.capture(self.credential, self.namespace, "k1", observation(1))
        self.store.fault = lambda point: (
            self.clock.advance(6000) if point == "after_journal" else None
        )
        with self.assertRaises(LocalError) as caught:
            self.store.purge(self.credential, first["observation"])
        self.assertEqual(caught.exception.code, "release_expired")
        self.store.fault = lambda _: None
        # Journalled and applied after the budget ran out; the store did not fence itself.
        self.assertTrue(self.store.healthy)
        applied = self.store.db.execute("SELECT value FROM meta WHERE key='journal'").fetchone()[0]
        self.assertEqual(int(applied), self.store.anchor["journal_sequence"])
        deleted = self.store.db.execute(
            "SELECT deleted FROM observations WHERE id=?", (first["observation"],)
        ).fetchone()[0]
        self.assertEqual(deleted, 1)
        self.store.capture(self.credential, self.namespace, "k2", observation(2))

    def test_other_failures_after_a_journal_write_still_fence_until_restart(self):
        first = self.store.capture(self.credential, self.namespace, "k1", observation(1))

        def crash(point):
            if point == "after_journal":
                raise RuntimeError("injected")

        self.store.fault = crash
        with self.assertRaises(RuntimeError):
            self.store.purge(self.credential, first["observation"])
        self.assertFalse(self.store.healthy)

    def test_a_failing_record_is_listed_without_content_and_nothing_is_skipped(self):
        self.append_while_closed(grant(), grant(tenant="ghost"))
        with self.assertRaises(LocalError) as caught:
            LocalStore(self.root, self.control, clock=self.clock)
        self.assertEqual(caught.exception.code, "journal_tenant_missing")
        # The first record committed on its own and stays applied; only the failing one is
        # pending. The listing is a read: it applies nothing.
        listing = LocalStore.pending_journal(self.root, self.control)
        self.assertEqual(listing["journal_records"], 2)
        self.assertEqual(listing["applied"], 1)
        self.assertEqual(
            [(item["sequence"], item["kind"], item["tenant"]) for item in listing["pending"]],
            [(2, "grant", "ghost")],
        )
        self.assertEqual(
            listing["pending"][0]["last_apply_error"]["error"], "journal_tenant_missing"
        )
        self.assertNotIn(SENTINEL, json.dumps(listing))
        self.assertNotIn(SENTINEL, (self.control / APPLY_FAILURE).read_text())
        # Opening again refuses for the same reason: no record is skipped.
        for _ in range(2):
            with self.assertRaises(LocalError) as again:
                LocalStore(self.root, self.control, clock=self.clock)
            self.assertEqual(again.exception.code, "journal_tenant_missing")
        self.assertEqual(LocalStore.pending_journal(self.root, self.control), listing)

    def test_listing_refuses_while_the_store_is_open_and_when_the_journal_is_rolled_back(self):
        with self.assertRaises(LocalError):
            LocalStore.pending_journal(self.root, self.control)
        self.store.close()
        self.assertEqual(LocalStore.pending_journal(self.root, self.control)["pending"], [])

    def test_a_recorded_failure_clears_when_the_record_applies(self):
        self.append_while_closed(grant(tenant="ghost"))
        with self.assertRaises(LocalError):
            LocalStore(self.root, self.control, clock=self.clock)
        self.assertTrue((self.control / APPLY_FAILURE).exists())
        Control(self.control).clear_apply_failure(1)
        self.assertFalse((self.control / APPLY_FAILURE).exists())

    def test_command_line_lists_pending_records(self):
        self.append_while_closed(grant(), grant(tenant="ghost"))
        with self.assertRaises(LocalError):
            LocalStore(self.root, self.control, clock=self.clock)
        done = subprocess.run(
            [
                sys.executable,
                "-m",
                "memoryking",
                "journal",
                "pending",
                "--store",
                str(self.root),
                "--control",
                str(self.control),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        listing = json.loads(done.stdout)
        self.assertEqual([item["sequence"] for item in listing["pending"]], [2])
        self.assertNotIn(SENTINEL, done.stdout)


if __name__ == "__main__":
    unittest.main()
