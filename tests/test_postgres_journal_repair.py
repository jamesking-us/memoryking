"""Shared journal replay: no request budget, progress per record, listing without content."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import psycopg
from test_journal_repair import SENTINEL, grant
from test_local_store import FakeClock

from memoryking.adapters.local_files import APPLY_FAILURE
from memoryking.adapters.postgres_admin import initialize_tenant, migrate, provision_roles
from memoryking.adapters.postgres_pool import create_pool
from memoryking.adapters.postgres_recovery import pending_journal
from memoryking.adapters.postgres_store import PostgresStore
from memoryking.adapters.shared_control import SharedControl
from memoryking.domain.local import LocalError
from tools.postgres_dev_cluster import DevCluster

BIN = Path(os.environ.get("MEMORYKING_PG_BIN", "tmp/postgres-dev/runtime/bin")).resolve()


@unittest.skipUnless((BIN / "pg_ctl").is_file(), "native PostgreSQL runtime not configured")
class PostgresJournalRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cluster = DevCluster(BIN)
        cls.directory = tempfile.TemporaryDirectory(prefix="mk-pg-jr-", dir="/tmp")
        cls.base = Path(cls.directory.name)
        try:
            with psycopg.connect(cls.cluster.dsn(), autocommit=True) as admin:
                provision_roles(admin)
            with psycopg.connect(cls.cluster.dsn("mk_migration"), autocommit=True) as migration:
                migrate(migration)
            cls.pool = create_pool(cls.cluster.dsn("mk_runtime"))
        except BaseException:
            cls.cluster.close()
            cls.directory.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.pool.close()
        cls.cluster.close()
        cls.directory.cleanup()

    def setUp(self):
        self.tenant = "t_" + uuid.uuid4().hex
        self.root, self.control = self.base / "content", self.base / "control"
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as migration:
            self.credential = initialize_tenant(
                migration, self.root, self.control, self.tenant
            ).read_text()
        self.clock = FakeClock()
        with self.store() as store:
            self.namespace = store.namespace(self.credential, "demo")

    def store(self, **kwargs):
        return PostgresStore(
            self.pool,
            self.root,
            self.control,
            self.tenant,
            self.credential,
            clock=self.clock,
            **kwargs,
        )

    def append_records(self, *actions):
        controller = SharedControl(self.control / self.tenant)
        with controller.locked():
            anchor = controller.read_anchor()
            for action in actions:
                controller.append(anchor, {**action, "tenant": action["tenant"]})

    def test_a_record_longer_than_a_request_budget_still_applies(self):
        with self.store() as store:
            store.grant(self.credential, "reader", "demo", ["read"])

        def crash(stage):
            if stage == "after_journal":
                raise RuntimeError("injected journal crash")

        with self.store(fault=crash) as store:
            with self.assertRaises(RuntimeError):
                store.revoke(self.credential, "reader", "demo")
        original = PostgresStore._apply

        def slow(store, record, at):
            self.clock.advance(20_000)  # four request budgets
            return original(store, record, at)

        with mock.patch.object(PostgresStore, "_apply", slow):
            with self.store() as store:
                store.status(self.credential, "demo")
        listing = pending_journal(self.pool, self.control, self.tenant)
        self.assertEqual(listing["pending"], [])
        self.assertEqual(listing["applied"], listing["journal_records"])

    def test_progress_survives_a_failing_record_and_nothing_is_skipped(self):
        self.append_records(grant(tenant=self.tenant), grant(tenant="ghost"))
        for _ in range(2):
            with self.store() as store:
                with self.assertRaises(LocalError) as caught:
                    store.status(self.credential, "demo")
                self.assertEqual(caught.exception.code, "journal_tenant_missing")
        listing = pending_journal(self.pool, self.control, self.tenant)
        # The first record committed in its own transaction and stays applied.
        self.assertEqual(listing["applied"], listing["journal_records"] - 1)
        self.assertEqual(
            [(item["kind"], item["tenant"]) for item in listing["pending"]], [("grant", "ghost")]
        )
        self.assertEqual(
            listing["pending"][0]["last_apply_error"]["error"], "journal_tenant_missing"
        )
        self.assertNotIn(SENTINEL, json.dumps(listing))
        self.assertNotIn(SENTINEL, (self.control / self.tenant / APPLY_FAILURE).read_text())
        # A reader is refused rather than served past an unapplied policy record.
        with self.store(release_only=True) as store:
            with self.assertRaises(LocalError) as caught:
                store.read(self.credential, "demo")
            self.assertEqual(caught.exception.code, "journal_replay_required")

    def test_a_deadline_inside_replay_leaves_earlier_records_applied(self):
        self.append_records(grant(tenant=self.tenant), grant(tenant=self.tenant, principal="b"))
        original = PostgresStore._apply
        calls = []

        def interrupted(store, record, at):
            calls.append(record["sequence"])
            if len(calls) == 2:
                raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
            return original(store, record, at)

        with mock.patch.object(PostgresStore, "_apply", interrupted):
            with self.store() as store:
                with self.assertRaises(psycopg.errors.QueryCanceled):
                    store.status(self.credential, "demo")
        listing = pending_journal(self.pool, self.control, self.tenant)
        self.assertEqual(len(listing["pending"]), 1)
        self.assertEqual(listing["pending"][0]["last_apply_error"]["error"], "statement_timeout")
        with self.store() as store:
            store.status(self.credential, "demo")
        self.assertEqual(pending_journal(self.pool, self.control, self.tenant)["pending"], [])
        self.assertFalse((self.control / self.tenant / APPLY_FAILURE).exists())

    def test_command_line_lists_pending_records(self):
        self.append_records(grant(tenant="ghost"))
        dsn_file = self.base / "runtime.dsn"
        dsn_file.write_text(self.cluster.dsn("mk_runtime"))
        dsn_file.chmod(0o600)
        done = subprocess.run(
            [
                sys.executable,
                "-m",
                "memoryking.shared",
                "journal-pending",
                "--dsn-file",
                str(dsn_file),
                "--control",
                str(self.control),
                "--tenant",
                self.tenant,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        listing = json.loads(done.stdout)
        self.assertEqual([item["tenant"] for item in listing["pending"]], ["ghost"])
        self.assertNotIn(SENTINEL, done.stdout)


if __name__ == "__main__":
    unittest.main()
