"""Independent operation journal survives native archive rollback and missing evidence."""

import json
import os
import subprocess
import sys

import psycopg
from test_postgres_parity import InspectionSQL, NativeFixture

from memoryking.adapters.postgres_recovery import backup, restore
from memoryking.adapters.postgres_store import PostgresStore


class PostgresOperationRecovery(NativeFixture):
    def backup(self):
        self.archive = self.root / "archive"
        with psycopg.connect(self.cluster.dsn("mk_backup"), autocommit=True) as connection:
            backup(self.store, connection, self.archive)

    def restore(self):
        self.store.close()
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as connection:
            self.owner = restore(
                connection, self.data, self.control, self.tenant, self.archive
            ).read_text()
        self.store = PostgresStore(self.pool, self.data, self.control, self.tenant, self.owner)
        self.store.db = InspectionSQL(self.store, self.store.db)

    def test_restored_dispatched_operation_requires_explicit_reconciliation(self):
        self.save()
        self.backup()
        old = self.store.incarnation
        self.intent()
        self.transition("dispatching", 1)
        self.restore()
        self.assertNotEqual(old, self.store.incarnation)
        operation = self.operations()[0]
        self.assertEqual("dispatching", operation["state"])
        self.assertTrue(operation["reconciliation_required"])
        self.fails("history_changed", self.resume)
        self.fails("checkpoint_reconciliation_required", lambda: self.save(revision=1))
        self.assign(writer="owner")
        self.fails("operation_identity_consumed", self.intent)
        self.fails("operation_reconciliation_required", lambda: self.transition("dispatching", 2))
        self.transition("succeeded", 2, reconciled=True)
        self.save(revision=2)
        self.assertEqual("ready", self.resume()["status"])

    def test_restore_retires_handoff_materialization_ack_and_resume(self):
        self.recipient = self.store.grant(self.owner, "reviewer", "p", ["read"])
        self.saved = self.save(readers=["owner", "reviewer"])
        prepared = self.prepare()
        delivered = self.view(prepared, raw=True)
        self.backup()
        self.restore()
        self.recipient = self.store.grant(self.owner, "reviewer", "p", ["read"])
        self.fails("history_changed", lambda: self.view(prepared))
        self.fails("history_changed", lambda: self.ack(prepared, delivered))
        self.fails("history_changed", self.resume)

    def test_missing_result_evidence_keeps_identity_but_requires_reconciliation(self):
        self.save()
        self.backup()
        self.intent()
        self.transition("dispatching", 1)
        late = self.store.capture(
            self.owner,
            self.ns,
            "late",
            {
                "version": 1,
                "project": "p",
                "source": "test",
                "source_event": "late",
                "text": "late external result",
                "readers": ["owner"],
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )
        self.transition(
            "succeeded", 2, evidence=[{"kind": "observation", "id": late["observation"]}]
        )
        self.restore()
        operation = self.operations()[0]
        self.assertEqual("succeeded", operation["state"])
        self.assertFalse(operation["evidence_available"])
        self.assertEqual([], operation["evidence"])
        self.assertIsNone(operation["result_hash"])
        self.assertTrue(operation["reconciliation_required"])
        self.fails("operation_reconciliation_required", lambda: self.transition("succeeded", 3))

    def test_actual_process_death_after_journal_replays_without_redispatch(self):
        self.save()
        command = self.command(
            "operation_intent",
            {
                **self.scope(),
                "operation": "op",
                "target_hash": "a" * 64,
                "parameters_hash": "b" * 64,
                "method": "manual",
                "provider_key": None,
            },
            "crash",
        )
        self.store.close()
        script = """
import json,os,sys
from pathlib import Path
from memoryking.adapters.postgres_pool import create_pool
from memoryking.adapters.postgres_store import PostgresStore
from memoryking.adapters.working import Working
pool=create_pool(sys.argv[1],size=1)
root,control,tenant=Path(sys.argv[2]),Path(sys.argv[3]),sys.argv[4]
credential=(control/tenant/'owner.credential').read_text()
with PostgresStore(pool,root,control,tenant,credential,fault=lambda stage:os._exit(71) if stage=='after_journal' else None) as store:
    Working(store).execute(credential,json.loads(sys.argv[5]))
"""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                self.cluster.dsn("mk_runtime"),
                str(self.data),
                str(self.control),
                self.tenant,
                json.dumps(command),
            ],
            env={k: v for k, v in os.environ.items() if k not in {"PYTHONHOME", "PYTHONPATH"}},
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(71, result.returncode, result.stderr)
        self.restart()
        self.assertEqual("intent_recorded", self.operations()[0]["state"])
        self.assertTrue(self.execute(command)["replayed"])
