"""P02 semantic assertions on native transactions; local IPC/SQLite mechanics excluded."""

import tempfile
import unittest
import uuid
from pathlib import Path

import psycopg
import test_local_store as local
from test_postgres_parity import InspectionSQL, NativeFixture, reuse

from memoryking.adapters.postgres_admin import initialize_tenant
from memoryking.adapters.postgres_store import PostgresStore


class ActorStore:
    """Fixture binds each different credential to a real separate PostgreSQL request."""

    def __init__(self, fixture, store):
        object.__setattr__(self, "fixture", fixture)
        object.__setattr__(self, "delegate", store)

    def __setattr__(self, name, value):
        setattr(self.delegate, name, value)

    def __getattr__(self, name):
        value = getattr(self.delegate, name)
        if name.startswith("_") or not callable(value):
            return value

        def invoke(*args, **kwargs):
            if (
                args
                and isinstance(args[0], str)
                and args[0] != self.fixture.credential
                and "." in args[0]
            ):
                with self.fixture.open(args[0]) as actor:
                    return getattr(actor, name)(*args, **kwargs)
            return value(*args, **kwargs)

        return invoke


class PostgresFoundationParity(unittest.TestCase):
    setUpClass = classmethod(NativeFixture.setUpClass.__func__)
    tearDownClass = classmethod(NativeFixture.tearDownClass.__func__)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mk-foundation-pg-", dir="/tmp")
        self.base = Path(self.temp.name)
        self.root, self.control = self.base / "db", self.base / "ctl"
        self.tenant = "t_" + uuid.uuid4().hex
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as migration:
            self.credential_path = initialize_tenant(
                migration, self.root, self.control, self.tenant
            )
        self.credential = self.credential_path.read_text()
        self.clock = local.FakeClock()
        self.store = ActorStore(self, self.open(self.credential))
        self.namespace = self.store.namespace(self.credential, "demo")

    def open(self, credential):
        store = PostgresStore(
            self.pool,
            self.root,
            self.control,
            self.tenant,
            credential,
            clock=self.clock,
            fault=self.store.fault if hasattr(self, "store") and self.store else None,
        )
        store.db = InspectionSQL(store, store.db)
        return store

    def restart(self):
        self.store.close()
        self.store = None
        self.store = ActorStore(self, self.open(self.credential))


reuse(PostgresFoundationParity, local.LocalTests, "tearDown capture error")
reuse(
    PostgresFoundationParity,
    local.LocalTests,
    """
test_capture_atomic_fts_occurrence_audit_and_replay
test_concurrent_equal_and_unequal_keys
test_authorization_scope_audience_and_current_retry_check
test_receipt_authorization_instant_and_release_deadline
test_validation_stall_rollback_and_temporal_inputs
test_backward_clock_fails_current_and_persisted_decisions
test_policy_commit_orders_before_next_delivery_gate
test_projection_gap_cancel_reindex_and_stale_fence
test_job_lease_expiry_retry_dead_letter_and_purge
test_queue_and_worker_bounds_and_current_source_permission
test_anchor_write_failure_returns_no_ack_and_retry_recovers
""",
)


def test_native_post_commit_deadline_and_counter_exhaustion(self):
    from memoryking.adapters.local_store import MAX_COUNTER

    self.capture()
    self.store.fault = lambda stage: self.clock.advance(5000) if stage == "after_commit" else None
    self.error("release_expired", self.store.read, self.credential, "demo")
    self.store.fault = lambda _: None
    self.assertEqual(1, self.store.db.execute("SELECT count(*) FROM receipts").fetchone()[0])
    self.store.db.execute("UPDATE tenants SET position=?", (MAX_COUNTER,))
    self.error("counter_exhausted", self.capture, 2)
    self.assertEqual(
        MAX_COUNTER, self.store.db.execute("SELECT position FROM tenants").fetchone()[0]
    )


PostgresFoundationParity.test_native_post_commit_deadline_and_counter_exhaustion = (
    test_native_post_commit_deadline_and_counter_exhaustion
)
