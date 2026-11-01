"""One semantic suite on two backends: every SQLite case runs on native PostgreSQL.

The knowledge, retrieval, working-state, handoff and repository cases are adopted in
full; a case that cannot run on PostgreSQL is named in LOCAL_ONLY with its reason. No
SQL or authorization is mocked. Transport and recovery scenarios have their own suites.
"""

import hashlib
import json
import os
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

import psycopg
import test_execution
import test_handoff
import test_knowledge
import test_repository
import test_retrieval
import test_working

from memoryking.adapters.execution import Execution
from memoryking.adapters.handoff import Handoff
from memoryking.adapters.knowledge_store import Knowledge
from memoryking.adapters.postgres_admin import initialize_tenant, migrate, provision_roles
from memoryking.adapters.postgres_pool import create_pool
from memoryking.adapters.postgres_store import PostgresStore
from memoryking.adapters.repository_store import Repository
from memoryking.adapters.retrieval import Retrieval
from memoryking.adapters.working import Working
from tools.postgres_dev_cluster import DevCluster

BIN = Path(os.environ.get("MEMORYKING_PG_BIN", "tmp/postgres-dev/runtime/bin")).resolve()


class InspectionSQL:
    """Test-only fixture inspection/corruption, still under a real exclusive DB gate."""

    def __init__(self, store, delegate):
        self.store, self.delegate = store, delegate

    def execute(self, query, args=()):
        if self.store.active:
            return self.delegate.execute(query, args)
        with self.store.transaction():
            return self.delegate.execute(query, args)

    def commit(self):
        pass  # The preceding fixture change already committed in its explicit transaction.


class NativeFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (BIN / "pg_ctl").is_file():
            raise unittest.SkipTest("native PostgreSQL runtime not configured")
        cls.cluster = DevCluster(BIN)
        try:
            with psycopg.connect(cls.cluster.dsn(), autocommit=True) as admin:
                provision_roles(admin)
            with psycopg.connect(cls.cluster.dsn("mk_migration"), autocommit=True) as migration:
                migrate(migration)
            cls.pool = create_pool(cls.cluster.dsn("mk_runtime"))
        except BaseException:
            cls.cluster.close()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.pool.close()
        cls.cluster.close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mk-pg-parity-", dir="/tmp")
        self.root = Path(self.temp.name)
        self.data, self.control = self.root / "data", self.root / "control"
        self.tenant = "t_" + uuid.uuid4().hex
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as migration:
            self.owner = initialize_tenant(
                migration, self.data, self.control, self.tenant
            ).read_text()
        self.store = PostgresStore(self.pool, self.data, self.control, self.tenant, self.owner)
        self.store.db = InspectionSQL(self.store, self.store.db)
        self.k = Knowledge(self.store)
        self.n = 0
        self.ns = self.store.namespace(self.owner, "p")
        self.raw = "Observed timeout=30. A result, not a universal rule."
        self.obs = self.store.capture(
            self.owner,
            self.ns,
            "e",
            {
                "version": 1,
                "project": "p",
                "source": "test",
                "source_event": "e",
                "text": self.raw,
                "readers": None,
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )
        self.span = {
            "observation": self.obs["observation"],
            "start_byte": 0,
            "end_byte": len(self.raw.encode()),
            "content_hash": hashlib.sha256(self.raw.encode()).hexdigest(),
        }

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    @contextmanager
    def actor(self, credential):
        if credential is None or credential == self.owner:
            yield self.store
        else:
            with PostgresStore(
                self.pool,
                self.data,
                self.control,
                self.tenant,
                credential,
                clock=self.store.clock,
                fault=self.store.fault,
            ) as store:
                yield store

    def service(self, adapter, command, credential=None, raw=False):
        with self.actor(credential) as store:
            output = []
            adapter(store).execute(credential or self.owner, command).emit(output.append)
            return output[0] if raw else json.loads(output[0])

    def run_k(self, action, args, credential=None, key=None):
        self.n += 1
        command = {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": action,
            "args": args,
        }
        if action in {"propose", "review", "attest", "promote"}:
            command["key"] = key or f"k{self.n}"
        return self.service(Knowledge, command, credential)

    def run_r(self, action="brief", args=None, credential=None, raw=False):
        return self.service(
            Retrieval,
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": action,
                "args": args or self.brief_args(),
            },
            credential,
            raw,
        )

    def execute(self, command, credential=None, raw=False):
        return self.service(Working, command, credential, raw)

    def run_h(self, action, args, credential=None, key=None, raw=False):
        self.n += 1
        return self.service(
            Handoff,
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": action,
                "args": args,
                "key": key or f"h{self.n}",
            },
            credential,
            raw,
        )

    def restart(self):
        clock = self.store.clock
        self.store.close()
        self.store = PostgresStore(
            self.pool, self.data, self.control, self.tenant, self.owner, clock=clock
        )
        self.store.db = InspectionSQL(self.store, self.store.db)
        self.k = Knowledge(self.store)

    def replay_result(self, credential, command):
        return self.service(Retrieval, command, credential)

    def replay(self, receipt, credential=None):
        return self.run_r(
            "replay", {"project": "p", "receipt": receipt}, credential or self.recipient, raw=True
        )

    def run_repo(self, action, credential=None, key=None, **args):
        self.n += 1
        command = {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": action,
            "args": {"project": "p", **args},
        }
        if action not in {"inspect", "impact", "coverage", "pending", "history"}:
            command["key"] = key or f"repo{self.n}"
        return self.service(Repository, command, credential)


def reuse(target, source, names):
    for name in names.split():
        setattr(target, name, getattr(source, name))


# Every SQLite case runs on PostgreSQL unless it is listed here with the reason it
# cannot: the semantic suite is opt-out, so a new case is parity-checked by default.
LOCAL_ONLY: dict[str, str] = {
    # SQLite file backup, restore and offline schema upgrades; PostgreSQL has its own
    # archive, restore and migration suites (test_postgres_store, test_postgres_migration).
    "test_backup_before_policy_fails_closed": "sqlite backup files",
    "test_evidence_revocation_purge_and_restore": "sqlite backup and restore",
    "test_purge_erases_handoff_copies_and_restore_retires_old_history": "sqlite restore",
    "test_backup_restore_retains_independent_operation_identity": "sqlite restore",
    "test_restore_retains_safe_operation_identity_when_result_source_was_lost": "sqlite restore",
    "test_migration_preserves_version_one_capture": "sqlite schema upgrade",
    "test_schema_three_migrates_without_rewriting_existing_observations": "sqlite schema upgrade",
    "test_schema_four_upgrade_preserves_checkpoint_and_adds_handoff": "sqlite schema upgrade",
    "test_schema_five_upgrade_preserves_existing_memory": "sqlite schema upgrade",
    # Crash recovery of the local journal and the recovery_required fence are exercised
    # on PostgreSQL by test_postgres_operation_recovery and test_postgres_store.
    "test_independent_journal_crash_replays_intent_and_original_retry": "sqlite journal crash",
    "test_unacknowledged_dispatch_is_recovered_as_ambiguous_after_deadline": "sqlite recovery fence",
    # Fixture SQL that only SQLite understands (PRAGMA table_info, zeroblob); the
    # behaviour itself is portable and covered by the adjacent adopted cases.
    "test_replacement_masks_count_toward_aggregate_work_limit": "sqlite fixture sql",
    "test_capacity_evicts_the_callers_oldest_artifact_and_expiry_erases_content": "sqlite fixture sql",
}


def adopt(target, source):
    """Copy every test of a SQLite case class onto a PostgreSQL fixture, minus LOCAL_ONLY."""
    for name in dir(source):
        if name.startswith("test_") and name not in LOCAL_ONLY:
            setattr(target, name, getattr(source, name))


reuse(NativeFixture, test_knowledge.KnowledgeTests, "body propose review query constraints")
reuse(
    NativeFixture,
    test_retrieval.RetrievalTests,
    "brief_args receipt clock fails constraint observe",
)
reuse(
    NativeFixture,
    test_working.WorkingTests,
    "scope command save resume assign intent transition operations summarize",
)
reuse(NativeFixture, test_handoff.HandoffTests, "prepare view ack")


class PostgresKnowledgeParity(NativeFixture):
    pass


adopt(PostgresKnowledgeParity, test_knowledge.KnowledgeTests)


class PostgresRetrievalParity(NativeFixture):
    pass


adopt(PostgresRetrievalParity, test_retrieval.RetrievalTests)


class PostgresWorkingParity(NativeFixture):
    pass


adopt(PostgresWorkingParity, test_working.WorkingTests)


class PostgresHandoffParity(NativeFixture):
    def setUp(self):
        super().setUp()
        self.recipient = self.store.grant(self.owner, "reviewer", "p", ["read"])
        self.other = self.store.grant(self.owner, "other", "p", ["read"])
        self.saved = self.save(readers=["owner", "reviewer"])


adopt(PostgresHandoffParity, test_handoff.HandoffTests)


class PostgresRepositoryParity(NativeFixture):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "code"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        (self.repo / "a.py").write_text("def value():\n    return 1\n")
        (self.repo / "b.py").write_text("other = 2\n")
        self.git("add", ".")
        self.git("commit", "-qm", "initial")
        self.rid = self.run_repo(
            "register", remote="local:fixture", clone="first", path=str(self.repo), readers=None
        )["repository"]
        self.first = self.capture()
        self.claim = self.propose()
        self.review(self.claim)


reuse(
    PostgresRepositoryParity,
    test_repository.RepositoryTests,
    "git capture binding bind verify worker",
)
adopt(PostgresRepositoryParity, test_repository.RepositoryTests)


class PostgresExecutionParity(NativeFixture):
    """Task activity snapshots over native PostgreSQL; readers open their own bound store."""

    def view(self, credential=None, offset=0):
        with self.actor(credential) as store, store.transaction() as decision:
            at = decision.evaluated_ms + offset
            who = store._auth(credential or self.owner, "read", "p", at)
            return Execution(store).snapshot(who, "p", at)


reuse(PostgresExecutionParity, test_execution.ExecutionTests, "passing event")
adopt(PostgresExecutionParity, test_execution.ExecutionTests)
