"""Native PostgreSQL service behavior and failure ordering, with non-owner pools."""

import concurrent.futures
import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

import psycopg
from test_local_store import FakeClock, delivered, observation

from memoryking.adapters.postgres_admin import initialize_tenant, migrate, provision_roles
from memoryking.adapters.postgres_pool import create_pool
from memoryking.adapters.postgres_store import PostgresStore
from memoryking.domain.local import LocalError
from tools.postgres_dev_cluster import DevCluster

BIN = Path(os.environ.get("MEMORYKING_PG_BIN", "tmp/postgres-dev/runtime/bin")).resolve()


@unittest.skipUnless((BIN / "pg_ctl").is_file(), "native PostgreSQL runtime not configured")
class PostgresStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Shared transport suites reuse this method without inheriting the class decorator.
        if not (BIN / "pg_ctl").is_file():
            raise unittest.SkipTest("native PostgreSQL runtime not configured")
        cls.cluster = DevCluster(BIN)
        cls.directory = tempfile.TemporaryDirectory(prefix="mk-pg-store-", dir="/tmp")
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
        self.clock = getattr(self, "clock_factory", FakeClock)()
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

    def capture(self, number=1, **kwargs):
        with self.store() as store:
            return store.capture(
                self.credential, self.namespace, f"key{number}", observation(number, **kwargs)
            )

    def test_capture_read_exact_retry_and_purge(self):
        first = self.capture()
        self.assertEqual(first, self.capture())
        with self.store(release_only=True) as store:
            packet = delivered(store.read(self.credential, "demo", query="durable"))
            self.assertEqual("durable memory", packet["items"][0]["text"])
        with self.store() as store:
            store.purge(self.credential, first["observation"])
        with self.store(release_only=True) as store:
            self.assertEqual([], delivered(store.read(self.credential, "demo"))["items"])
        self.assertEqual([], list((self.root / "blobs" / self.tenant).iterdir()))

    def test_concurrent_retry_and_contiguous_clock(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: self.capture(), range(16)))
        self.assertTrue(all(result == results[0] for result in results))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda n: self.capture(n), range(2, 10)))
        self.assertEqual(list(range(2, 10)), sorted(result["position"] for result in results))
        with self.store() as store:
            with store.transaction():
                self.assertEqual(9, store.db.execute("SELECT count(*) FROM events").fetchone()[0])

    def test_journal_failure_replayed_before_subsequent_release(self):
        self.capture()
        with self.store() as store:
            other = store.grant(self.credential, "reader", "demo", ["read"])

        def crash(stage):
            if stage == "after_journal":
                raise RuntimeError("injected journal crash")

        with self.store(fault=crash) as store:
            with self.assertRaises(RuntimeError):
                store.revoke(self.credential, "reader", "demo")
        with PostgresStore(
            self.pool,
            self.root,
            self.control,
            self.tenant,
            other,
            clock=self.clock,
            release_only=True,
        ) as store:
            with self.assertRaises(LocalError) as caught:
                store.read(other, "demo")
            self.assertEqual("journal_replay_required", caught.exception.code)
        with self.store() as store:
            store.status(self.credential, "demo")
        with PostgresStore(
            self.pool,
            self.root,
            self.control,
            self.tenant,
            other,
            clock=self.clock,
            release_only=True,
        ) as store:
            with self.assertRaises(LocalError) as caught:
                store.read(other, "demo")
            self.assertEqual("denied", caught.exception.code)

    def test_shared_release_orders_before_waiting_revocation(self):
        self.capture()
        with self.store() as store:
            reader = store.grant(self.credential, "reader", "demo", ["read"])
        reached, proceed = threading.Event(), threading.Event()

        def pause(stage):
            if stage == "before_commit":
                reached.set()
                if not proceed.wait(3):
                    raise RuntimeError("test barrier timed out")

        def read():
            with PostgresStore(
                self.pool,
                self.root,
                self.control,
                self.tenant,
                reader,
                clock=self.clock,
                release_only=True,
                fault=pause,
            ) as store:
                return delivered(store.read(reader, "demo"))

        def revoke():
            with self.store() as store:
                store.revoke(self.credential, "reader", "demo")

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(read)
            self.assertTrue(reached.wait(3))
            second = executor.submit(revoke)
            self.assertFalse(second.done())
            proceed.set()
            self.assertEqual(1, len(first.result()["items"]))
            second.result()
        with PostgresStore(
            self.pool,
            self.root,
            self.control,
            self.tenant,
            reader,
            clock=self.clock,
            release_only=True,
        ) as store:
            with self.assertRaises(LocalError):
                store.read(reader, "demo")

    def test_anchor_detects_acknowledged_database_rollback(self):
        self.capture()
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as migration:
            with migration.transaction():
                migration.execute("SELECT set_config('memoryking.tenant',%s,true)", (self.tenant,))
                migration.execute(
                    "UPDATE memoryking.tenant_meta SET value='0' WHERE tenant=%s AND key='commit_serial'",
                    (self.tenant,),
                )
        with self.store() as store:
            with self.assertRaises(LocalError) as caught:
                store.status(self.credential, "demo")
            self.assertEqual("recovery_required", caught.exception.code)

    def test_pool_scrubs_aborted_transaction_and_session_identity(self):
        pool = create_pool(self.cluster.dsn("mk_runtime"), size=1)
        try:
            connection = pool.getconn()
            connection.execute("SELECT set_config('memoryking.tenant',%s,false)", (self.tenant,))
            connection.execute("BEGIN")
            with self.assertRaises(psycopg.errors.DivisionByZero):
                connection.execute("SELECT 1/0")
            pool.putconn(connection)
            with pool.connection() as clean:
                self.assertIn(
                    clean.execute("SELECT current_setting('memoryking.tenant',true)").fetchone()[0],
                    (None, ""),
                )
                self.assertEqual(
                    [], clean.execute("SELECT tenant FROM memoryking.tenants").fetchall()
                )
        finally:
            pool.close()

    def test_pool_exhaustion_is_bounded_pressure_without_mutation(self):
        import time

        for role in ("mk_runtime", "mk_worker"):
            with self.subTest(role=role):
                pool = create_pool(self.cluster.dsn(role), size=1, timeout=1)
                held = pool.getconn()
                try:
                    before = (self.control / self.tenant / "anchor").read_bytes()
                    with self.assertRaises(LocalError) as caught:
                        PostgresStore(pool, self.root, self.control, self.tenant, self.credential)
                    self.assertEqual("service_busy", caught.exception.code)
                    # A request deadline shorter than the pool's own wait fails at the
                    # deadline; one already past fails before any wait at all.
                    # A generous deadline never waits past the pool's own one-second limit.
                    for deadline, limit in ((0.3, 0.9), (-1.0, 0.1), (30.0, 1.9)):
                        started = time.monotonic()
                        with self.assertRaises(LocalError) as caught:
                            PostgresStore(
                                pool,
                                self.root,
                                self.control,
                                self.tenant,
                                self.credential,
                                request_deadline=time.monotonic() + deadline,
                            )
                        self.assertEqual("service_busy", caught.exception.code)
                        self.assertLess(time.monotonic() - started, limit)
                    self.assertEqual(before, (self.control / self.tenant / "anchor").read_bytes())
                    pool.putconn(held)
                    held = None
                    with PostgresStore(pool, self.root, self.control, self.tenant, self.credential):
                        pass
                finally:
                    if held is not None:
                        pool.putconn(held)
                    pool.close()
        first = self.capture()
        self.assertEqual(first, self.capture())
        self.assertEqual(1, first["position"])

    def test_closed_pool_is_unavailable_without_dependency_details(self):
        pool = create_pool(self.cluster.dsn("mk_runtime"), size=1)
        pool.close()
        with self.assertRaises(LocalError) as caught:
            PostgresStore(pool, self.root, self.control, self.tenant, self.credential)
        self.assertEqual("service_unavailable", caught.exception.code)

    def test_drain_completes_audit_jobs_without_a_worker(self):
        self.capture()
        with self.store() as store:
            self.assertEqual(store.drain(), 1)
            self.assertEqual(store.drain(), 0)
            status = store.status(self.credential, "demo")
        self.assertEqual(status["jobs"], {"complete": 1})
        self.assertEqual(status["projection"]["frontier"], status["position"])

    def test_end_maintenance_clears_only_the_matching_fence_for_a_current_admin(self):
        from memoryking.adapters.postgres_recovery import end_maintenance
        from memoryking.adapters.shared_control import SharedControl

        self.capture()
        with self.store() as store:
            reader = store.grant(self.credential, "reader", "demo", ["read"])
        operation = "ab" * 16
        controller = SharedControl(self.control / self.tenant)
        with controller.locked():
            anchor = controller.read_anchor()
            anchor.update(state="maintenance", maintenance_operation=operation)
            controller.anchor(anchor)
        with self.assertRaises(LocalError) as caught:
            with self.store() as store:
                store.read(self.credential, "demo")
        self.assertEqual("recovery_required", caught.exception.code)
        with psycopg.connect(self.cluster.dsn("mk_runtime"), autocommit=True) as connection:
            for wrong_operation, credential, code in (
                ("cd" * 16, self.credential, "maintenance_operation_mismatch"),
                (operation, reader, "denied"),
                (operation, "not-a-token", "invalid_token"),
            ):
                with self.assertRaises(LocalError) as caught:
                    end_maintenance(
                        connection, self.control, self.tenant, wrong_operation, credential
                    )
                self.assertEqual(code, caught.exception.code)
            self.assertEqual(controller.read_anchor()["state"], "maintenance")
            result = end_maintenance(
                connection, self.control, self.tenant, operation, self.credential
            )
        self.assertEqual(
            {"status": "active", "tenant": self.tenant, "operation": operation}, result
        )
        self.assertNotIn("maintenance_operation", controller.read_anchor())
        with self.store() as store:
            self.assertEqual(
                "durable memory", delivered(store.read(self.credential, "demo"))["items"][0]["text"]
            )
        with psycopg.connect(self.cluster.dsn("mk_runtime"), autocommit=True) as connection:
            with self.assertRaises(LocalError) as caught:
                end_maintenance(connection, self.control, self.tenant, operation, self.credential)
            self.assertEqual("maintenance_operation_mismatch", caught.exception.code)

    def test_native_backup_restore_retires_history_and_replays_revocation(self):
        from memoryking.adapters.postgres_recovery import backup, restore

        first = self.capture()
        with self.store() as store:
            reader = store.grant(self.credential, "reader", "demo", ["read"])
            old_cursor = delivered(store.read(self.credential, "demo"))["cursor"]
            old_lease = store.claim(self.credential, "demo")["lease"]
        archive = self.base / (self.tenant + "-backup")
        with (
            self.store() as store,
            psycopg.connect(self.cluster.dsn("mk_backup"), autocommit=True) as connection,
        ):
            manifest = backup(store, connection, archive)
        self.assertEqual("memoryking-postgres-1", manifest["format"])
        lost = self.capture(2)
        with self.store() as store:
            store.revoke(self.credential, "reader", "demo")
        old_credential, old_namespace = self.credential, self.namespace
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as connection:
            self.credential = restore(
                connection, self.root, self.control, self.tenant, archive, clock=self.clock
            ).read_text()
        with self.store() as store:
            self.namespace = store.namespace(self.credential, "demo")
            self.assertEqual(
                "durable memory", delivered(store.read(self.credential, "demo"))["items"][0]["text"]
            )
            with self.assertRaises(LocalError) as caught:
                store.status(self.credential, "demo", lost["watermark"])
            self.assertEqual("history_changed", caught.exception.code)
            for number in (1, 2):  # Surviving and lost retry-ledger entries.
                with self.assertRaises(LocalError) as caught:
                    store.capture(
                        self.credential, old_namespace, f"key{number}", observation(number)
                    )
                self.assertEqual("history_changed", caught.exception.code)
            with self.assertRaises(LocalError) as caught:
                store.read(self.credential, "demo", cursor=old_cursor)
            self.assertEqual("history_changed", caught.exception.code)
            with self.assertRaises(LocalError) as caught:
                store.finish(self.credential, "demo", old_lease, "complete")
            self.assertEqual("history_changed", caught.exception.code)
            with store.transaction():
                self.assertIsNone(
                    store.db.execute(
                        "SELECT principal FROM grants WHERE principal='reader'"
                    ).fetchone()
                )
        self.capture(3)
        replacement = self.capture(4)
        self.assertGreater(replacement["position"], lost["position"])
        with self.store() as store:
            with self.assertRaises(LocalError) as caught:
                store.status(self.credential, "demo", lost["watermark"])
            self.assertEqual("history_changed", caught.exception.code)
            with self.assertRaises(LocalError):
                store.status(self.credential, "demo", first["watermark"])
        for credential in (old_credential, reader):
            with self.assertRaises(LocalError):
                PostgresStore(
                    self.pool, self.root, self.control, self.tenant, credential, clock=self.clock
                )

    def test_restore_interruption_stays_fenced_then_explicit_retry_recovers(self):
        from memoryking.adapters.postgres_recovery import backup, restore

        self.capture()
        archive = self.base / (self.tenant + "-backup")
        with (
            self.store() as store,
            psycopg.connect(self.cluster.dsn("mk_backup"), autocommit=True) as connection,
        ):
            backup(store, connection, archive)

        def crash(stage):
            if stage == "restore_before_activation":
                raise RuntimeError("injected before activation")

        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as connection:
            with self.assertRaises(RuntimeError):
                restore(
                    connection,
                    self.root,
                    self.control,
                    self.tenant,
                    archive,
                    clock=self.clock,
                    fault=crash,
                )
        with self.assertRaises(LocalError):
            with self.store() as store:
                store.status(self.credential, "demo")
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as connection:
            self.credential = restore(
                connection, self.root, self.control, self.tenant, archive, clock=self.clock
            ).read_text()
        with self.store() as store:
            self.assertEqual(1, len(delivered(store.read(self.credential, "demo"))["items"]))

    def test_lost_anchor_requires_independently_retained_current_journal_head(self):
        from memoryking.adapters.postgres_recovery import backup, restore

        self.capture()
        archive = self.base / (self.tenant + "-lost-anchor")
        with (
            self.store() as store,
            psycopg.connect(self.cluster.dsn("mk_backup"), autocommit=True) as connection,
        ):
            backup(store, connection, archive)
            store.grant(self.credential, "reader", "demo", ["read"])
            head = store.anchor["journal_head"]
        (self.control / self.tenant / "anchor").unlink()
        with self.assertRaises(LocalError):
            self.store()
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as connection:
            with self.assertRaises(LocalError) as caught:
                restore(
                    connection,
                    self.root,
                    self.control,
                    self.tenant,
                    archive,
                    clock=self.clock,
                    lost_anchor_journal_head="0" * 64,
                )
            self.assertEqual("recovery_checkpoint_invalid", caught.exception.code)
            self.credential = restore(
                connection,
                self.root,
                self.control,
                self.tenant,
                archive,
                clock=self.clock,
                lost_anchor_journal_head=head,
            ).read_text()
        with self.store() as store:
            self.assertTrue(store.anchor["history_loss"])
            with store.transaction():
                self.assertIsNotNone(
                    store.db.execute(
                        "SELECT principal FROM grants WHERE principal='reader'"
                    ).fetchone()
                )

    def test_archive_corruption_and_weakened_durability_reject_before_activation(self):
        from memoryking.adapters.local_files import atomic
        from memoryking.adapters.postgres_recovery import backup, restore

        self.capture()
        archive = self.base / (self.tenant + "-corrupt")
        with (
            self.store() as store,
            psycopg.connect(self.cluster.dsn("mk_backup"), autocommit=True) as connection,
        ):
            backup(store, connection, archive)
        original = (archive / "observations.ndjson").read_bytes()
        atomic(archive / "observations.ndjson", original + b"\n")
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as connection:
            with self.assertRaises(LocalError) as caught:
                restore(connection, self.root, self.control, self.tenant, archive, clock=self.clock)
            self.assertEqual("backup_invalid", caught.exception.code)
            atomic(archive / "observations.ndjson", original)
            connection.execute("SET synchronous_commit=off")
            with self.assertRaises(LocalError) as caught:
                restore(connection, self.root, self.control, self.tenant, archive, clock=self.clock)
            self.assertEqual("database_durability_required", caught.exception.code)
        with self.store() as store:
            self.assertEqual("active", store.anchor["state"])
            self.assertEqual(1, len(delivered(store.read(self.credential, "demo"))["items"]))
