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
LATER_TABLES = {
    "credential_generations",
    "lexical_state",
    "lexical_projects",
    "lexical_terms",
    "supersessions",
    "retirement_requests",
    "pins",
    "consolidation_runs",
    "unreviewed_tier",
}


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

    def test_restore_refuses_a_migration_role_that_bypasses_row_security(self):
        from memoryking.adapters.postgres_recovery import backup, restore

        self.capture()
        archive = self.base / (self.tenant + "-bypass")
        with (
            self.store() as store,
            psycopg.connect(self.cluster.dsn("mk_backup"), autocommit=True) as connection,
        ):
            backup(store, connection, archive)
        with psycopg.connect(self.cluster.dsn(), autocommit=True) as admin:
            for attribute, undo in (("BYPASSRLS", "NOBYPASSRLS"), ("SUPERUSER", "NOSUPERUSER")):
                admin.execute(f"ALTER ROLE mk_migration {attribute}")
                try:
                    with psycopg.connect(
                        self.cluster.dsn("mk_migration"), autocommit=True
                    ) as connection:
                        with self.assertRaises(LocalError) as caught:
                            restore(
                                connection,
                                self.root,
                                self.control,
                                self.tenant,
                                archive,
                                clock=self.clock,
                            )
                    self.assertEqual("migration_role_required", caught.exception.code)
                finally:
                    admin.execute(f"ALTER ROLE mk_migration {undo}")
        self.capture(2)  # The refused restore changed nothing.

    def test_expire_results_states_its_tenant_in_every_write(self):
        from unittest import mock

        from memoryking.adapters.postgres_sql import PostgresSQL

        self.capture()
        seen = []
        original = PostgresSQL.execute

        def record(db, statement, parameters=()):
            seen.append((statement, tuple(parameters)))
            return original(db, statement, parameters)

        with self.store() as store, mock.patch.object(PostgresSQL, "execute", record):
            store.expire_results()
        writes = [item for item in seen if item[0].startswith(("UPDATE retries", "DELETE FROM"))]
        self.assertGreaterEqual(len(writes), 5)
        for statement, parameters in writes:
            self.assertIn("tenant=?", statement)
            self.assertIn(self.tenant, parameters)

    def test_restore_leaves_other_tenants_rows_alone(self):
        from memoryking.adapters.postgres_recovery import backup, restore

        other = "t_" + uuid.uuid4().hex
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as migration:
            other_credential = initialize_tenant(
                migration, self.root, self.control, other
            ).read_text()
        with PostgresStore(
            self.pool, self.root, self.control, other, other_credential, clock=self.clock
        ) as store:
            other_namespace = store.namespace(other_credential, "demo")
            store.capture(other_credential, other_namespace, "key1", observation(1))
        self.capture()
        archive = self.base / (self.tenant + "-isolation")
        with (
            self.store() as store,
            psycopg.connect(self.cluster.dsn("mk_backup"), autocommit=True) as connection,
        ):
            backup(store, connection, archive)
        counts = (
            "SELECT (SELECT count(*) FROM memoryking.retries WHERE tenant=%(t)s AND result IS NOT NULL),"
            "(SELECT count(*) FROM memoryking.receipts WHERE tenant=%(t)s),"
            "(SELECT count(*) FROM memoryking.tenant_meta WHERE tenant=%(t)s AND key='incarnation' AND value=%(i)s)"
        )
        with psycopg.connect(self.cluster.dsn(), autocommit=True) as admin:
            incarnation = admin.execute(
                "SELECT value FROM memoryking.tenant_meta WHERE tenant=%s AND key='incarnation'",
                (other,),
            ).fetchone()[0]
            before = admin.execute(counts, {"t": other, "i": incarnation}).fetchone()
        self.assertEqual(1, before[0])
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as connection:
            restore(connection, self.root, self.control, self.tenant, archive, clock=self.clock)
        with psycopg.connect(self.cluster.dsn(), autocommit=True) as admin:
            self.assertEqual(
                before, admin.execute(counts, {"t": other, "i": incarnation}).fetchone()
            )

    def test_a_delegated_token_dies_when_its_issuer_or_its_subject_is_rotated(self):
        # Decision: a delegated token acts as the issuer and also records the named
        # subject's generation at delegation, so rotating either retires it.
        def usable(token):
            try:
                with PostgresStore(
                    self.pool, self.root, self.control, self.tenant, token, clock=self.clock
                ) as store:
                    with store.transaction() as decision:
                        store._auth(token, "read", "demo", decision.evaluated_ms)
            except LocalError as exc:
                return exc.code
            return None

        def delegate(issuer):
            with PostgresStore(
                self.pool, self.root, self.control, self.tenant, issuer, clock=self.clock
            ) as store:
                return store.delegate("subject", "demo", ["read"], 60_000)

        with self.store() as store:
            issuer = store.grant(self.credential, "issuer", "demo", ["read", "capture"])
            subject = store.grant(self.credential, "subject", "demo", ["read"])
        delegated = delegate(issuer)
        self.assertIsNone(usable(delegated))
        with self.store() as store:
            issuer = store.grant(
                self.credential, "issuer", "demo", ["read", "capture"], rotate=True
            )
        self.assertEqual("credential_replaced", usable(delegated))
        # The rotated issuer delegates again; the new token works.
        delegated = delegate(issuer)
        self.assertIsNone(usable(delegated))
        with self.store() as store:
            subject = store.grant(self.credential, "subject", "demo", ["read"], rotate=True)
        self.assertEqual("credential_replaced", usable(delegated))
        self.assertIsNone(usable(subject))
        delegated = delegate(issuer)
        self.assertIsNone(usable(delegated))
        # Revoking the issuer's access retires the delegation as well.
        with self.store() as store:
            store.revoke(self.credential, "issuer", "demo")
        self.assertEqual("denied", usable(delegated))

    def test_archive_commits_to_a_blob_index_and_legacy_embedded_list_restores(self):
        from memoryking.adapters.local_files import atomic
        from memoryking.adapters.postgres_recovery import backup, restore

        self.capture(1, text="first distinct body")
        self.capture(2, text="second distinct body")
        archive = self.base / (self.tenant + "-index-backup")
        with (
            self.store() as store,
            psycopg.connect(self.cluster.dsn("mk_backup"), autocommit=True) as connection,
        ):
            manifest = backup(store, connection, archive)
            control = store.control
        self.assertEqual(2, manifest["blob_index"]["count"])
        self.assertNotIn("blobs", manifest)
        self.assertEqual(12, manifest["schema"])
        index = (archive / "blob-index").read_bytes()
        digests = index.decode().splitlines()

        def restore_archive():
            with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as connection:
                return restore(
                    connection, self.root, self.control, self.tenant, archive, clock=self.clock
                ).read_text()

        atomic(archive / "blob-index", "".join(d + "\n" for d in digests[:1]).encode())
        with self.assertRaises(LocalError) as caught:
            restore_archive()
        self.assertEqual("backup_invalid", caught.exception.code)
        atomic(archive / "blob-index", index)
        from memoryking.adapters.postgres_recovery import archived_blob

        blob = archived_blob(archive, digests[0])
        original = blob.read_bytes()
        atomic(blob, original + b"x")
        with self.assertRaises(LocalError) as caught:
            restore_archive()
        self.assertEqual("backup_invalid", caught.exception.code)
        atomic(blob, original)
        # The layout of the previous release: the list embedded in the signed manifest.
        legacy = dict(manifest)
        legacy.pop("blob_index")
        legacy.update(schema=6, blobs=digests)
        legacy["tables"] = {
            name: meta for name, meta in manifest["tables"].items() if name not in LATER_TABLES
        }
        atomic(archive / "manifest", control.sign(legacy).encode())
        (archive / "blob-index").unlink()
        # Archives of the previous release also kept every blob flat under blobs/.
        for digest in digests:
            sharded = archived_blob(archive, digest)
            os.rename(sharded, archive / "blobs" / digest)
            sharded.parent.rmdir()
        self.credential = restore_archive()
        with self.store() as store:
            self.namespace = store.namespace(self.credential, "demo")
            self.assertEqual(2, len(delivered(store.read(self.credential, "demo"))["items"]))

    def archive(self, name, **kwargs):
        from memoryking.adapters.postgres_recovery import backup

        with (
            self.store() as store,
            psycopg.connect(self.cluster.dsn("mk_backup"), autocommit=True) as connection,
        ):
            return backup(store, connection, self.base / (self.tenant + name), **kwargs)

    def test_backup_fences_the_tenant_only_while_the_snapshot_is_taken(self):
        from unittest import mock

        from memoryking.adapters import blobs as blobfiles
        from memoryking.adapters.postgres_recovery import restore

        self.capture(1, text="before the backup")
        seen = []
        real = blobfiles.copy_verified

        def during_copy(source, destination, expected):
            if not seen:
                # Requests run while blob files are copied: the fence is already gone.
                with self.store() as store:
                    seen.append(
                        store.capture(
                            self.credential,
                            self.namespace,
                            "mid",
                            observation(2, text="during the copy"),
                        )
                    )
                    seen.append(store.anchor["state"])
            return real(source, destination, expected)

        with mock.patch.object(blobfiles, "copy_verified", during_copy):
            manifest = self.archive("-brief")
        self.assertEqual("active", seen[1])
        self.assertEqual(1, manifest["blob_index"]["count"])
        # The archive is the snapshot: the capture made during the copy is not in it.
        archive = self.base / (self.tenant + "-brief")
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as connection:
            self.credential = restore(
                connection, self.root, self.control, self.tenant, archive, clock=self.clock
            ).read_text()
        with self.store() as store, store.transaction():
            texts = [
                r[0]
                for r in store.db.execute(
                    "SELECT count(*) FROM observations WHERE blob IS NOT NULL"
                )
            ]
        self.assertEqual([1], texts)

    def test_an_incremental_archive_links_verified_blobs_and_restores_on_its_own(self):
        import shutil

        from memoryking.adapters import blobs as blobfiles
        from memoryking.adapters.postgres_recovery import archived_blob, restore

        self.capture(1, text="first distinct body")
        self.capture(2, text="second distinct body")
        first = self.archive("-a")
        self.capture(3, text="third distinct body")
        # A damaged copy in the earlier archive is not linked, whatever its name.
        digest = first and (self.base / (self.tenant + "-a") / "blob-index").read_text().split()[0]
        damaged = archived_blob(self.base / (self.tenant + "-a"), digest)
        damaged.unlink()
        damaged.write_bytes(b"damaged")
        damaged.chmod(0o600)
        second = self.archive("-b", previous=self.base / (self.tenant + "-a"))
        self.assertEqual((1, 2), (second["blobs_linked"], second["blobs_copied"]))
        self.assertEqual(
            digest, blobfiles.sha256_file(archived_blob(self.base / (self.tenant + "-b"), digest))
        )
        shutil.rmtree(self.base / (self.tenant + "-a"))
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as connection:
            self.credential = restore(
                connection,
                self.root,
                self.control,
                self.tenant,
                self.base / (self.tenant + "-b"),
                clock=self.clock,
            ).read_text()
        with self.store() as store, store.transaction():
            self.assertEqual(
                3,
                store.db.execute(
                    "SELECT count(*) FROM observations WHERE blob IS NOT NULL"
                ).fetchone()[0],
            )

    def test_purge_deletes_its_own_blob_and_the_sweep_is_explicit(self):
        from unittest import mock

        from memoryking.adapters import blobs as blobfiles
        from memoryking.adapters.local_files import atomic

        first = self.capture(1, text="shared content")
        self.capture(2, text="shared content")
        orphan = self.root / "blobs" / self.tenant / "ab" / ("ab" + "0" * 62)
        orphan.parent.mkdir(mode=0o700, exist_ok=True)
        atomic(orphan, b"left behind")
        name = __import__("hashlib").sha256(b"shared content").hexdigest()
        with (
            mock.patch.object(PostgresStore, "gc", side_effect=AssertionError("no sweep")),
            self.store() as store,
        ):
            store.purge(self.credential, first["observation"])
            self.assertTrue(blobfiles.blob_path(self.root, self.tenant, name).is_file())
            with store.transaction():
                other = store.db.execute(
                    "SELECT id FROM observations WHERE blob=? AND deleted=0", (name,)
                ).fetchone()[0]
            store.purge(self.credential, other)
        self.assertFalse(blobfiles.blob_path(self.root, self.tenant, name).is_file())
        self.assertTrue(orphan.exists())
        with self.store() as store:
            self.assertEqual(1, store.gc())
        self.assertFalse(orphan.exists())

    def test_job_position_index_exists_and_reindex_backlog_does_not_refuse_capture(self):
        from memoryking.application import store_core

        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as connection:
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM pg_indexes WHERE schemaname='memoryking' "
                    "AND indexname='jobs_position'"
                ).fetchone()
            )
        original, store_core.QUEUE_LIMIT = store_core.QUEUE_LIMIT, 3
        try:
            for number in (1, 2, 3):
                self.capture(number)
            with self.assertRaises(LocalError) as caught:
                self.capture(4)
            self.assertEqual("queue_full", caught.exception.code)
            with self.store() as store:
                store.reindex(self.credential)
            self.capture(4)  # the reindexed jobs are backlog, not new captures
        finally:
            store_core.QUEUE_LIMIT = original

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

    def test_database_ahead_of_the_control_journal_fails_closed(self):
        control = self.control / self.tenant
        stale = {name: (control / name).read_bytes() for name in ("journal", "anchor")}
        with self.store() as store:
            store.grant(self.credential, "reader", "demo", ["read"])
        # A journal tail lost after the database commit, or a control tree copied from
        # before it: the database has applied a record the journal does not hold.
        for name, data in stale.items():
            (control / name).write_bytes(data)
        with self.assertRaises(LocalError) as caught:
            with self.store() as store:
                store.status(self.credential, "demo")
        self.assertEqual("journal_rollback", caught.exception.code)

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
