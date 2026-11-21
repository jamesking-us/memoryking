"""The operations log, compaction and instance lease on PostgreSQL, plus `on_behalf_of`.

The store-level cases of test_operations_log.py run unchanged on the shared profile; the
cases below are the ones that only make sense there (several processes sharing a tenant,
delegated credentials, the command line).
"""

import json
import shutil
import subprocess
import sys
import unittest

import psycopg
import test_operations_log as local
import test_postgres_parity as parity
from test_local_store import FakeClock

from memoryking.adapters.knowledge_store import Knowledge
from memoryking.adapters.postgres_recovery import backup, restore
from memoryking.adapters.postgres_store import PostgresStore
from memoryking.adapters.team import activity, export
from memoryking.adapters.timeline import timeline


class PostgresOperationsLog(local.OperationsLogTests, parity.NativeFixture):
    setUp = parity.NativeFixture.setUp
    tearDown = parity.NativeFixture.tearDown
    fails = parity.NativeFixture.fails
    clock = parity.NativeFixture.clock
    scope = parity.NativeFixture.scope
    command = parity.NativeFixture.command
    execute = parity.NativeFixture.execute
    save = parity.NativeFixture.save
    intent = parity.NativeFixture.intent
    transition = parity.NativeFixture.transition
    operations = parity.NativeFixture.operations

    def build(self, **kwargs):
        store = PostgresStore(self.pool, self.data, self.control, self.tenant, self.owner, **kwargs)
        store.db = parity.InspectionSQL(store, store.db)
        self.k = Knowledge(store)
        return store

    def archive(self, destination):
        with psycopg.connect(self.cluster.dsn("mk_backup"), autocommit=True) as connection:
            backup(self.store, connection, destination)

    def restore_from(self, archive):
        self.store.close()
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as connection:
            self.owner = restore(
                connection,
                self.data,
                self.control,
                self.tenant,
                archive,
                clock=self.store.clock,
            ).read_text()
        self.store = self.build(clock=self.store.clock)

    def tree(self):
        return self.store.control_path

    def copy_to(self, copy):
        shutil.copytree(self.control, copy / "control")

    def build_copy(self, copy):
        return PostgresStore(
            self.pool,
            self.data,
            copy / "control",
            self.tenant,
            self.owner,
            clock=self.store.clock,
        )

    def lease_sql(self, statement):
        """Read or change the lease row without a store, so the read claims nothing."""
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as connection:
            with connection.transaction():
                connection.execute("SET LOCAL search_path=memoryking,pg_catalog")
                connection.execute("SELECT set_config('memoryking.tenant',%s,true)", (self.tenant,))
                result = connection.execute(statement)
                return result.fetchone() if result.description else None

    def lease_row(self):
        row = self.lease_sql("SELECT holder,renewed_ms FROM instance_lease")
        return None if row is None else (row[0], row[1])

    def drop_lease(self):
        self.lease_sql("DELETE FROM instance_lease")

    def test_processes_sharing_a_control_tree_co_hold_the_lease(self):
        clock = self.clock()
        other = self.build(clock=clock)
        try:
            self.assertEqual(other.lease, self.store.lease)
            for store in (self.store, other, self.store):
                self.touch(store)
            self.assertEqual(self.lease_row()[0], self.store.lease)
        finally:
            other.close()

    def test_a_reader_never_writes_or_claims_the_lease(self):
        clock = self.clock()
        self.touch()
        holder, renewed = self.lease_row()
        clock.advance(30_000)
        with self.build(clock=clock, release_only=True) as reader:
            self.touch(reader)
        self.assertEqual(self.lease_row(), (holder, renewed))
        # A refused holder is refused for reads too.
        self.reopen(instance_id="alpha")
        self.fails("control_lease_held", self.touch)
        with self.build(clock=clock, instance_id="alpha", release_only=True) as reader:
            self.fails("control_lease_held", lambda: self.touch(reader))

    def test_a_refused_holder_applies_nothing(self):
        self.clock()
        self.touch()
        self.reopen(instance_id="alpha")
        before = self.store.control.operations.verified_count(self.store.anchor)
        self.fails("control_lease_held", self.touch)
        self.assertEqual(self.store.control.operations.verified_count(self.store.anchor), before)

    def test_the_command_line_names_the_instance_and_compacts_against_an_archive_directory(self):
        self.save()
        self.life("op1")
        self.archive(self.root / "archives" / "a")
        self.drop_lease()
        dsn = self.root / "runtime.dsn"
        dsn.write_text(self.cluster.dsn("mk_runtime"))
        dsn.chmod(0o600)
        credential = self.root / "owner.credential"
        credential.write_text(self.owner)
        credential.chmod(0o600)
        done = subprocess.run(
            [
                sys.executable,
                "-m",
                "memoryking.shared",
                "compact-operations",
                "--dsn-file",
                str(dsn),
                "--content",
                str(self.data),
                "--control",
                str(self.control),
                "--tenant",
                self.tenant,
                "--credential",
                str(credential),
                "--archive-dir",
                str(self.root / "archives"),
                "--instance-id",
                "cli",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        # One active segment is never dropped, and nothing here is sealed yet.
        self.assertEqual(json.loads(done.stdout), {"segments_dropped": 0, "status": "complete"})
        # The command ran as instance "cli": this process, a different holder, is now refused.
        self.fails("control_lease_held", self.touch)


class PostgresDelegatedActorTests(parity.NativeFixture):
    """A delegated token acts as its issuer; the subject it acts for is recorded on the rows."""

    def setUp(self):
        parity.NativeFixture.setUp(self)
        self.store.clock = FakeClock()
        self.issuer = self.store.grant(self.owner, "issuer", "p", ["read", "capture"])
        self.subject = self.store.grant(self.owner, "subject", "p", ["read", "capture"])
        with PostgresStore(
            self.pool,
            self.data,
            self.control,
            self.tenant,
            self.issuer,
            clock=self.store.clock,
        ) as store:
            self.delegated = store.delegate("subject", "p", ["capture"], 600_000)

    def capture_as(self, credential, event):
        with PostgresStore(
            self.pool,
            self.data,
            self.control,
            self.tenant,
            credential,
            clock=self.store.clock,
        ) as store:
            namespace = store.namespace(credential, "p")
            return store.capture(
                credential,
                namespace,
                event,
                {
                    "version": 1,
                    "project": "p",
                    "source": "test",
                    "source_event": event,
                    "text": "written " + event,
                    "readers": None,
                    "classification": "internal",
                    "observed_at_ms": 1,
                },
            )

    def rows(self, table, column):
        with self.store.transaction():
            return [
                tuple(row)
                for row in self.store.db.execute(
                    f"SELECT {column},on_behalf_of FROM {table} WHERE tenant=? ORDER BY 1",
                    (self.tenant,),
                ).fetchall()
            ]

    def test_events_and_receipts_of_a_delegated_write_name_the_subject_and_others_do_not(self):
        self.capture_as(self.issuer, "plain")
        self.capture_as(self.delegated, "delegated")
        events = self.store.db.execute(
            "SELECT actor,on_behalf_of,kind FROM events WHERE tenant=? AND kind='observation_captured' ORDER BY position",
            (self.tenant,),
        ).fetchall()
        self.assertEqual(
            [tuple(row)[:2] for row in events[-2:]], [("issuer", None), ("issuer", "subject")]
        )
        # Receipts written under a delegated token carry it too.
        with PostgresStore(
            self.pool, self.data, self.control, self.tenant, self.delegated, clock=self.store.clock
        ) as store:
            with store.transaction():
                store.db.execute(
                    "INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (self.tenant, "rcpt_d", "issuer", store.incarnation, 1, 2, 0, 0, "h", "[]"),
                )
        with self.store.transaction():
            kept = self.store.db.execute(
                "SELECT principal,on_behalf_of FROM receipts WHERE id='rcpt_d'"
            ).fetchone()
        self.assertEqual(tuple(kept), ("issuer", "subject"))

    def test_the_team_activity_timeline_and_audit_export_show_who_it_was_for(self):
        self.capture_as(self.issuer, "plain")
        self.capture_as(self.delegated, "delegated")
        with self.store.transaction() as decision:
            who = self.store._auth(self.owner, "admin", "*", decision.evaluated_ms)
            at = decision.evaluated_ms
            seen = timeline(self.store, who, "p")["items"]
            team = activity(self.store, who, "p", at)
            audit = export(self.store, who, "p", at)
        marked = [item for item in seen if item.get("on_behalf_of")]
        self.assertEqual([(i["actor"], i["on_behalf_of"]) for i in marked], [("issuer", "subject")])
        self.assertTrue(all("on_behalf_of" not in i for i in seen if i not in marked))
        issuer = next(p for p in team["principals"] if p["principal"] == "issuer")
        self.assertEqual(issuer["acted_for"], {"subject": 1})
        self.assertEqual(
            [(e["actor"], e["on_behalf_of"]) for e in audit["events"] if e.get("on_behalf_of")],
            [("issuer", "subject")],
        )

    def test_journal_replay_never_carries_the_current_requests_subject(self):
        self.capture_as(self.delegated, "one")
        with self.store.transaction():
            rows = self.store.db.execute(
                "SELECT kind,on_behalf_of FROM events WHERE tenant=? AND kind='grant'",
                (self.tenant,),
            ).fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(row[1] is None for row in rows))


if __name__ == "__main__":
    unittest.main()
