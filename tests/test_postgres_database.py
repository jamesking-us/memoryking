"""Actual PostgreSQL role/RLS checks; skip explicitly when native runtime is absent."""

import os
import tempfile
import unittest
from pathlib import Path

import psycopg

from memoryking.adapters.postgres_admin import initialize_tenant, migrate, provision_roles
from tools.postgres_dev_cluster import DevCluster

BIN = Path(os.environ.get("MEMORYKING_PG_BIN", "tmp/postgres-dev/runtime/bin")).resolve()


@unittest.skipUnless((BIN / "pg_ctl").is_file(), "native PostgreSQL runtime not configured")
class PostgresDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cluster = DevCluster(BIN)
        cls.directory = tempfile.TemporaryDirectory(prefix="mk-pg-files-", dir="/tmp")
        cls.base = Path(cls.directory.name)
        try:
            with psycopg.connect(cls.cluster.dsn(), autocommit=True) as admin:
                provision_roles(admin)
            with psycopg.connect(cls.cluster.dsn("mk_migration"), autocommit=True) as migration:
                migrate(migration)
                for tenant in ("alpha", "beta"):
                    initialize_tenant(migration, cls.base / "content", cls.base / "control", tenant)
        except BaseException:
            cls.cluster.close()
            cls.directory.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.cluster.close()
        cls.directory.cleanup()

    def connection(self, role="mk_runtime"):
        connection = psycopg.connect(self.cluster.dsn(role), autocommit=True)
        connection.execute("SET search_path=memoryking,pg_catalog")
        return connection

    def test_rls_default_deny_and_transaction_scope(self):
        with self.connection() as connection:
            self.assertEqual([], connection.execute("SELECT tenant FROM tenants").fetchall())
            for tenant in ("alpha", "beta", "alpha"):
                with connection.transaction():
                    connection.execute("SELECT set_config('memoryking.tenant',%s,true)", (tenant,))
                    self.assertEqual(
                        [(tenant,)], connection.execute("SELECT tenant FROM tenants").fetchall()
                    )
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        with connection.transaction():
                            connection.execute("INSERT INTO tenants VALUES ('other',0,0,0)")
                self.assertEqual([], connection.execute("SELECT tenant FROM tenants").fetchall())
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                connection.execute("TRUNCATE tenants CASCADE")
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                connection.execute("CREATE TABLE memoryking.unwanted(id int)")
            row = connection.execute(
                "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
            ).fetchone()
            self.assertEqual((False, False), row)

    def test_worker_and_backup_roles_are_distinct(self):
        with self.connection("mk_worker") as worker:
            with worker.transaction():
                worker.execute("SELECT set_config('memoryking.tenant','alpha',true)")
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    with worker.transaction():
                        worker.execute("DELETE FROM grants")
        with self.connection("mk_backup") as backup:
            self.assertEqual(
                [("alpha",), ("beta",)],
                backup.execute("SELECT tenant FROM tenants ORDER BY tenant").fetchall(),
            )
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                backup.execute("DELETE FROM tenants")

    def test_tenant_foreign_key_and_forced_owner_rls(self):
        with self.connection("mk_migration") as migration:
            self.assertEqual([], migration.execute("SELECT tenant FROM tenants").fetchall())
        with self.connection() as connection:
            with connection.transaction():
                connection.execute("SELECT set_config('memoryking.tenant','alpha',true)")
                with self.assertRaises(psycopg.errors.ForeignKeyViolation):
                    with connection.transaction():
                        connection.execute(
                            "INSERT INTO lexical VALUES ('alpha','nonexistent','secret')"
                        )

    def test_runtime_durability_and_owner_rejection(self):
        from memoryking.adapters.postgres_pool import check
        from memoryking.domain.local import LocalError

        with self.connection() as runtime:
            check(runtime)
            runtime.execute("SET synchronous_commit=off")
            with self.assertRaises(LocalError) as caught:
                check(runtime)
            self.assertEqual("database_durability_required", caught.exception.code)
        with self.connection("mk_migration") as migration:
            with self.assertRaises(LocalError) as caught:
                check(migration)
            self.assertEqual("runtime_table_owner_forbidden", caught.exception.code)

    def test_real_read_replica_cannot_authorize_release(self):
        import subprocess

        from memoryking.adapters.postgres_pool import check
        from memoryking.domain.local import LocalError

        with tempfile.TemporaryDirectory(prefix="mk-replica-", dir="/tmp") as directory:
            root = Path(directory)
            data, socket = root / "data", root / "socket"
            socket.mkdir(mode=0o700)
            subprocess.run(
                [
                    str(BIN / "pg_basebackup"),
                    "-D",
                    str(data),
                    "-h",
                    str(self.cluster.socket),
                    "-U",
                    "mk_dev_admin",
                    "-X",
                    "stream",
                    "-R",
                    "--checkpoint=fast",
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            command = [str(BIN / "pg_ctl"), "-D", str(data)]
            subprocess.run(
                command
                + [
                    "-l",
                    str(root / "server.log"),
                    "-o",
                    f"-c listen_addresses='' -c unix_socket_directories={socket} -c unix_socket_permissions=0700",
                    "-w",
                    "-t",
                    "30",
                    "start",
                ],
                check=True,
                capture_output=True,
                timeout=35,
            )
            try:
                with psycopg.connect(
                    host=str(socket), user="mk_runtime", dbname="postgres", autocommit=True
                ) as replica:
                    self.assertTrue(replica.execute("SELECT pg_is_in_recovery()").fetchone()[0])
                    with self.assertRaises(LocalError) as caught:
                        check(replica)
                    self.assertEqual(
                        "primary_nonprivileged_runtime_required", caught.exception.code
                    )
            finally:
                subprocess.run(
                    command + ["-m", "immediate", "-w", "stop"],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
