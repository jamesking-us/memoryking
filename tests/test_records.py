"""The record-type registry: the lists derived from it are the lists they replaced, every table of
both backends is declared, and the logic that has per-type rules covers the same types."""

import os
import re
import tempfile
import unittest
from pathlib import Path

from memoryking.adapters.local_store import LocalStore
from memoryking.application import records
from memoryking.application.records import (
    BY_TABLE,
    RECORD_TYPES,
    archive_tables,
    capacity_counters,
    dependency_tables,
    partition_tables,
    retire_for_new_history,
    tables_of,
    worker_tables,
)
from memoryking.application.retention import RETENTION_RULES
from memoryking.application.store_core import CAPACITY_LIMITS, StoreCore

# What each archive schema added to the shared backup, as written out before the registry
# existed. Every archive written by an older release must stay restorable, so the derived
# inventories must equal these exactly, in this order.
ARCHIVE_ADDITIONS = {
    1: (
        "tenants",
        "grants",
        "events",
        "observations",
        "lexical",
        "retries",
        "jobs",
        "projections",
        "projection_items",
        "receipts",
        "retired_sources",
        "knowledge_state",
        "families",
        "revisions",
        "evidence_spans",
        "lifecycle",
        "replacements",
        "conflicts",
        "attestations",
        "rendered_artifacts",
        "checkpoint_streams",
        "checkpoints",
        "operation_intents",
        "handoffs",
        "handoff_views",
        "repositories",
        "repository_clones",
        "repository_snapshots",
        "code_policies",
        "code_validations",
        "repository_jobs",
        "tenant_gates",
        "tenant_meta",
    ),
    2: (
        "embedding_partitions",
        "embedding_jobs",
        "embedding_items",
        "processing_usage",
        "adapter_bases",
    ),
    3: ("experience_records", "ranking_state", "ranking_history"),
    4: ("egress_audit", "session_summaries"),
    5: ("source_trust", "observation_holds"),
    6: ("anchors",),
    7: (),
    8: ("credential_generations",),
    9: ("lexical_state", "lexical_projects", "lexical_terms"),
    10: ("supersessions", "retirement_requests", "pins"),
    11: (),
    12: ("consolidation_runs", "unreviewed_tier"),
    13: ("embedding_vectors", "embedding_skips"),
}

# The tables the shared worker role was granted write access to before the registry.
WORKER_TABLES = {
    "jobs",
    "projections",
    "projection_items",
    "repository_jobs",
    "embedding_jobs",
    "embedding_items",
    "embedding_vectors",
    "embedding_skips",
    "processing_usage",
    "code_validations",
    "meta",
}

# The statements a restore ran, one by one, before the registry derived them.
NEW_HISTORY_STATEMENTS = {
    "UPDATE retries SET result=NULL",
    "DELETE FROM instance_lease",
    "DELETE FROM receipts",
    "UPDATE rendered_artifacts SET state='retired',body=NULL,request_json=NULL,diagnostics=NULL,dependencies='[]'",
    "UPDATE checkpoints SET state='retired',body=NULL,dependencies='[]'",
    "UPDATE handoffs SET state='retired'",
    "UPDATE ranking_state SET configuration=NULL,version=version+1",
    "DELETE FROM adapter_bases",
    "DELETE FROM embedding_items",
    "DELETE FROM embedding_vectors",
    "DELETE FROM embedding_skips",
    "DELETE FROM embedding_jobs",
    "UPDATE embedding_partitions SET incarnation=?,state='disabled',generation=1,scheduled=0,frontier=0",
}


class Recorder:
    def __init__(self):
        self.statements = []

    def execute(self, statement, params=()):
        self.statements.append((statement, tuple(params)))


class RegistryDerivationTests(unittest.TestCase):
    def test_archive_inventories_are_the_ones_the_literals_listed(self):
        inventory: tuple[str, ...] = ()
        for schema, added in ARCHIVE_ADDITIONS.items():
            inventory += added
            self.assertEqual(inventory, archive_tables(schema), f"archive schema {schema}")
        self.assertEqual(records.ARCHIVE_SCHEMA, max(ARCHIVE_ADDITIONS))
        from memoryking.adapters import postgres_recovery as recovery

        self.assertEqual(archive_tables(13), recovery.TABLES)
        self.assertEqual(archive_tables(1), recovery.TABLES_V1)
        self.assertEqual(archive_tables(12), recovery.TABLES_V12)

    def test_worker_and_partition_lists(self):
        self.assertEqual(WORKER_TABLES, set(worker_tables()))
        self.assertEqual(
            ("embedding_jobs", "embedding_items", "embedding_vectors"), partition_tables()
        )

    def test_new_history_runs_the_statements_a_restore_ran(self):
        local, shared = Recorder(), Recorder()
        retire_for_new_history(local, "hist-1")
        retire_for_new_history(shared, "hist-1", "tenant-a")
        self.assertEqual(NEW_HISTORY_STATEMENTS, {s for s, _ in local.statements})
        self.assertEqual(len(NEW_HISTORY_STATEMENTS), len(local.statements))
        self.assertEqual(
            {s + (" WHERE tenant=?") for s in NEW_HISTORY_STATEMENTS},
            {s for s, _ in shared.statements},
        )
        for statement, params in shared.statements:
            self.assertEqual(
                ("hist-1", "tenant-a") if "embedding_partitions" in statement else ("tenant-a",),
                params,
            )
        for statement, params in local.statements:
            self.assertEqual(("hist-1",) if "embedding_partitions" in statement else (), params)

    def test_every_new_history_type_has_a_statement(self):
        declared = {r.table for r in RECORD_TYPES if r.new_history is not None}
        delete = {r.table for r in RECORD_TYPES if r.new_history == "delete"}
        self.assertEqual(declared - delete, set(records.NEW_HISTORY_STATEMENTS))


class RegistryCoversLogicTests(unittest.TestCase):
    def test_purge_follows_the_types_that_declare_dependencies(self):
        self.assertEqual(set(dependency_tables()), set(StoreCore._FOLLOW))
        for method in StoreCore._FOLLOW.values():
            self.assertTrue(callable(getattr(StoreCore, method)))

    def test_capacity_counters_are_the_declared_ones(self):
        self.assertEqual(set(CAPACITY_LIMITS), set(capacity_counters()))
        self.assertEqual(len(capacity_counters()), len(set(capacity_counters())))

    def test_retention_rules_are_the_declared_ones(self):
        declared = {r.table: r.retention for r in RECORD_TYPES if r.retention is not None}
        self.assertEqual(declared, RETENTION_RULES)

    def test_declarations_are_consistent(self):
        for record in RECORD_TYPES:
            self.assertEqual(record, BY_TABLE[record.table])
            if record.scope == "none":
                self.assertIsNone(record.archived_in, record.table)
        archived = [r.archived_in for r in RECORD_TYPES if r.archived_in is not None]
        self.assertEqual(sorted(archived), archived, "archive order follows archive schemas")


class SQLiteSchemaTests(unittest.TestCase):
    def test_every_sqlite_table_is_declared_and_every_declared_one_exists(self):
        with tempfile.TemporaryDirectory(prefix="mk-rec-", dir="/tmp") as name:
            base = Path(name)
            LocalStore.initialize(base / "db", base / "ctl")
            with LocalStore(base / "db", base / "ctl") as store:
                found = {
                    row[0]
                    for row in store.db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
        self.assertEqual(set(), found - set(BY_TABLE), "tables the registry does not name")
        self.assertEqual(set(), tables_of("local") - found, "declared tables the schema lacks")


WRITE = re.compile(r"^\s*(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+(\w+)", re.I)


class Spy:
    """Forwards to the real connection and remembers every statement."""

    def __init__(self, delegate):
        self.delegate, self.statements = delegate, []

    def execute(self, statement, params=()):
        self.statements.append(statement)
        return self.delegate.execute(statement, params)

    def __getattr__(self, name):
        return getattr(self.delegate, name)


class PurgeCoverageTests(unittest.TestCase):
    def test_a_purge_writes_only_to_types_that_declare_it(self):
        with tempfile.TemporaryDirectory(prefix="mk-rec-", dir="/tmp") as name:
            base = Path(name)
            credential = LocalStore.initialize(base / "db", base / "ctl").read_text()
            with LocalStore(base / "db", base / "ctl") as store:
                namespace = store.namespace(credential, "demo")
                result = store.capture(
                    credential,
                    namespace,
                    "k1",
                    {
                        "version": 1,
                        "project": "demo",
                        "source": "test",
                        "source_event": "e1",
                        "text": "durable memory",
                        "readers": None,
                        "classification": "internal",
                        "observed_at_ms": 0,
                    },
                )
                spy = Spy(store.db)
                store.db = spy
                store.purge(credential, result["observation"])
                store.db = spy.delegate
        written = {m.group(1) for s in spy.statements if (m := WRITE.match(s))}
        declared = {r.table for r in RECORD_TYPES if r.purge is not None}
        # The meta counters advance with every applied record.
        self.assertEqual(set(), written - declared - {"meta"}, "purge wrote to an undeclared type")
        always = {
            "observations",
            "lexical",
            "anchors",
            "projection_items",
            "embedding_items",
            "embedding_vectors",
            "embedding_jobs",
            "adapter_bases",
            "retries",
            "jobs",
            "receipts",
            "tenants",
            "retired_sources",
            "events",
        }
        self.assertEqual(set(), always - written, "a declared purge step did not run")
        for table in always:
            self.assertIsNotNone(BY_TABLE[table].purge, table)


BIN = Path(os.environ.get("MEMORYKING_PG_BIN", "tmp/postgres-dev/runtime/bin")).resolve()


@unittest.skipUnless((BIN / "pg_ctl").is_file(), "native PostgreSQL runtime not configured")
class PostgresSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg

        from memoryking.adapters.postgres_admin import migrate, provision_roles
        from tools.postgres_dev_cluster import DevCluster

        cls.cluster = DevCluster(BIN)
        try:
            with psycopg.connect(cls.cluster.dsn(), autocommit=True) as admin:
                provision_roles(admin)
            with psycopg.connect(cls.cluster.dsn("mk_migration"), autocommit=True) as migration:
                migrate(migration)
        except BaseException:
            cls.cluster.close()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.cluster.close()

    def catalog(self, statement):
        import psycopg

        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as connection:
            return connection.execute(statement).fetchall()

    def test_every_shared_table_is_declared_and_every_declared_one_exists(self):
        tables = {
            r[0]
            for r in self.catalog(
                "SELECT tablename FROM pg_tables WHERE schemaname='memoryking' "
                "UNION SELECT viewname FROM pg_views WHERE schemaname='memoryking'"
            )
        }
        self.assertEqual(set(), tables - set(BY_TABLE), "tables the registry does not name")
        self.assertEqual(set(), tables_of("shared") - tables, "declared tables the schema lacks")

    def test_every_tenant_scoped_table_forces_row_level_security(self):
        tenant = {r.table for r in RECORD_TYPES if r.scope == "tenant" and r.table != "meta"}
        rows = self.catalog(
            "SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,"
            "(SELECT count(*) FROM pg_policies p WHERE p.schemaname='memoryking' "
            "AND p.tablename=c.relname AND p.policyname='tenant_isolation') "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='memoryking' AND c.relkind='r'"
        )
        by_name = {r[0]: r for r in rows}
        for table in sorted(tenant & set(tables_of("shared"))):
            if table in {"embedding_native"}:
                continue
            self.assertIn(table, by_name, table)
            _, enabled, forced, policies = by_name[table]
            self.assertTrue(enabled and forced and policies >= 1, f"{table}: row-level security")
        for table in by_name:
            if table in BY_TABLE and BY_TABLE[table].scope == "none":
                continue
            self.assertIn(table, tenant, f"{table} is tenant scoped in the schema")

    def test_worker_grants_follow_the_registry(self):
        rows = self.catalog(
            "SELECT table_name FROM information_schema.role_table_grants "
            "WHERE table_schema='memoryking' AND grantee='mk_worker' AND privilege_type='INSERT'"
        )
        granted = {r[0] for r in rows}
        self.assertTrue((set(worker_tables()) - {"meta"}) <= granted)
        self.assertEqual(
            set(), (granted - set(worker_tables())) - {"receipts", "receipt_observations"}
        )


if __name__ == "__main__":
    unittest.main()
