"""Shared-profile credential generations at the edges: restore, maintenance, owner rotation.

The generation rules inside `_auth` run on both backends in test_credentials; these cases
cover the PostgreSQL-only paths that mint or verify a credential outside `_auth`.
"""

import hmac
import json
import unittest
from pathlib import Path

import psycopg
import test_postgres_store
from test_local_store import delivered

from memoryking.adapters.local_files import atomic
from memoryking.adapters.postgres_recovery import backup, end_maintenance, restore, rotate_owner
from memoryking.adapters.postgres_store import PostgresStore
from memoryking.adapters.shared_control import SharedControl
from memoryking.application.records import archive_tables
from memoryking.domain.local import LocalError, SystemClock


class PostgresCredentialTests(unittest.TestCase):
    clock_factory = SystemClock
    setUpClass = classmethod(test_postgres_store.PostgresStoreTests.setUpClass.__func__)
    tearDownClass = classmethod(test_postgres_store.PostgresStoreTests.tearDownClass.__func__)
    setUp = test_postgres_store.PostgresStoreTests.setUp
    store = test_postgres_store.PostgresStoreTests.store
    capture = test_postgres_store.PostgresStoreTests.capture

    def body(self, token):
        return SharedControl(self.control / self.tenant).verify(token)

    def rotate(self):
        """Rotate the operator credential; returns the credential it replaced."""
        old = self.credential
        path = rotate_owner(self.pool, self.root, self.control, self.tenant, clock=self.clock)
        self.credential = path.read_text()
        return old

    def check(self, token, capability="read", project="demo"):
        """None when the credential authorizes the action, else the refusal code."""
        try:
            with self.store() as store:
                with store.transaction() as decision:
                    store._auth(token, capability, project, decision.evaluated_ms)
        except LocalError as exc:
            return exc.code
        return None

    def backup_to(self, name):
        archive = self.base / (self.tenant + name)
        with (
            self.store() as store,
            psycopg.connect(self.cluster.dsn("mk_backup"), autocommit=True) as connection,
        ):
            backup(store, connection, archive)
            return archive, store.control

    def restore_from(self, archive):
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as connection:
            return restore(
                connection, self.root, self.control, self.tenant, archive, clock=self.clock
            )

    def forged(self, **fields):
        controller = SharedControl(self.control / self.tenant)
        current = self.body(self.credential)
        return controller.sign({**current, **fields})

    def test_restore_returns_a_working_credential_for_an_admin_at_generation_two(self):
        first = self.rotate()
        second = self.rotate()
        self.assertEqual(2, self.body(self.credential)["generation"])
        self.capture()
        archive, _ = self.backup_to("-gen2")
        before = self.body(self.credential)
        self.credential = self.restore_from(archive).read_text()
        after = self.body(self.credential)
        self.assertEqual(2, after["generation"])
        self.assertNotEqual(before["incarnation"], after["incarnation"])
        self.assertIsNone(self.check(self.credential, "admin", "*"))
        with self.store() as store:
            self.assertEqual(1, len(delivered(store.read(self.credential, "demo"))["items"]))
        # Credentials from before the restore are refused by the new incarnation's rules.
        for old in (first, second):
            with self.assertRaises(LocalError) as caught:
                self.store_for(old)
            self.assertEqual("history_changed", caught.exception.code)
        # A credential minted for the new incarnation at an older generation is replaced.
        for token in (self.forged_without_generation(), self.forged(generation=1)):
            self.assertEqual("credential_replaced", self.check(token, "admin", "*"))

    def forged_without_generation(self):
        fields = dict(self.body(self.credential))
        fields.pop("generation", None)
        return SharedControl(self.control / self.tenant).sign(fields)

    def store_for(self, credential):
        return PostgresStore(
            self.pool, self.root, self.control, self.tenant, credential, clock=self.clock
        )

    def test_restore_at_generation_zero_mints_the_same_credential_as_before(self):
        self.capture()
        archive, controller = self.backup_to("-gen0")
        self.credential = self.restore_from(archive).read_text()
        body = self.body(self.credential)
        self.assertNotIn("generation", body)
        expected = controller.sign(
            {
                "kind": "credential",
                "store": controller.store,
                "incarnation": body["incarnation"],
                "tenant": self.tenant,
                "principal": body["principal"],
                "audience": "memoryking-shared",
            }
        )
        self.assertTrue(hmac.compare_digest(expected, self.credential))
        self.assertIsNone(self.check(self.credential, "admin", "*"))

    def test_a_rotation_after_the_backup_is_replayed_and_the_restored_credential_follows(self):
        self.capture()
        archive, _ = self.backup_to("-late")
        self.rotate()
        self.credential = self.restore_from(archive).read_text()
        self.assertEqual(1, self.body(self.credential)["generation"])
        self.assertIsNone(self.check(self.credential, "admin", "*"))

    def test_archives_before_credential_generations_restore_with_every_generation_zero(self):
        self.capture(1, text="first distinct body")
        self.capture(2, text="second distinct body")
        with self.store() as store:
            agent = store.grant(self.credential, "agent", "demo", ["read"])
        archive, controller = self.backup_to("-legacy")
        manifest = controller.verify_document(archive / "manifest")
        digests = (archive / "blob-index").read_bytes().decode().splitlines()
        for schema in (7, 6):
            tables = {
                name: meta
                for name, meta in manifest["tables"].items()
                if name in archive_tables(schema)
            }
            self.assertEqual(set(archive_tables(schema)), set(tables))
            legacy = dict(manifest)
            legacy.update(schema=schema, tables=tables)
            if schema == 6:
                legacy.pop("blob_index")
                legacy["blobs"] = digests
            atomic(archive / "manifest", controller.sign(legacy).encode())
            self.credential = self.restore_from(archive).read_text()
            self.assertNotIn("generation", self.body(self.credential))
            with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as connection:
                with connection.transaction():
                    connection.execute(
                        "SELECT set_config('memoryking.tenant',%s,true)", (self.tenant,)
                    )
                    count = connection.execute(
                        "SELECT count(*) FROM memoryking.credential_generations WHERE tenant=%s",
                        (self.tenant,),
                    ).fetchone()[0]
            self.assertEqual(0, count)
            self.assertIsNone(self.check(self.credential, "admin", "*"))
            with self.store() as store:
                self.assertEqual(2, len(delivered(store.read(self.credential, "demo"))["items"]))
            # The agent's pre-restore credential belongs to the old incarnation.
            with self.assertRaises(LocalError) as caught:
                self.store_for(agent)
            self.assertEqual("history_changed", caught.exception.code)

    def fence(self, operation):
        controller = SharedControl(self.control / self.tenant)
        with controller.locked():
            anchor = controller.read_anchor()
            anchor.update(state="maintenance", maintenance_operation=operation)
            controller.anchor(anchor)
        return controller

    def test_end_maintenance_refuses_a_rotated_away_credential(self):
        old = self.rotate()
        operation = "ab" * 16
        controller = self.fence(operation)
        ghost = SharedControl(self.control / self.tenant).sign(
            {**self.body(self.credential), "principal": "ghost", "generation": 7}
        )
        with psycopg.connect(self.cluster.dsn("mk_runtime"), autocommit=True) as connection:
            for credential, code in (
                (old, "credential_replaced"),
                (self.forged(generation=5), "credential_replaced"),
                (self.forged(generation=True), "credential_replaced"),
                (self.forged(generation="1"), "credential_replaced"),
                # No current grant: refused exactly as before, never told it was replaced.
                (ghost, "denied"),
            ):
                with self.assertRaises(LocalError) as caught:
                    end_maintenance(connection, self.control, self.tenant, operation, credential)
                self.assertEqual(code, caught.exception.code)
            self.assertEqual("maintenance", controller.read_anchor()["state"])
            result = end_maintenance(
                connection, self.control, self.tenant, operation, self.credential
            )
        self.assertEqual("active", result["status"])
        self.assertEqual("active", controller.read_anchor()["state"])

    def test_a_revoked_and_regranted_admin_is_restored_with_its_generation(self):
        # The recovery admin may be an ordinary named principal rather than the owner.
        with self.store() as store:
            store.grant(self.credential, "second", "demo", ["read"])
            store.revoke(self.credential, "second", "demo")
            token = store.grant(self.credential, "second", "demo", ["read"])
        self.assertEqual(1, self.body(token)["generation"])
        archive, _ = self.backup_to("-regrant")
        self.credential = self.restore_from(archive).read_text()
        with self.store() as store:
            again = store.grant(self.credential, "second", "demo", ["read"])
        self.assertEqual(1, self.body(again)["generation"])

    def test_rotate_owner_replaces_the_file_and_retires_the_old_credential(self):
        old = self.rotate()
        path = self.control / self.tenant / "owner.credential"
        self.assertEqual(self.credential, path.read_text())
        self.assertFalse((self.control / self.tenant / "owner.credential.next").exists())
        self.assertEqual(1, self.body(self.credential)["generation"])
        self.assertNotIn("generation", self.body(old))
        self.assertEqual("credential_replaced", self.check(old, "admin", "*"))
        self.assertIsNone(self.check(self.credential, "admin", "*"))
        with self.store() as store:
            self.assertEqual([], delivered(store.read(self.credential, "demo"))["items"])
        self.rotate()
        self.assertEqual(2, self.body(self.credential)["generation"])

    def test_a_crash_after_the_commit_is_completed_by_running_it_again(self):
        def crash(point):
            if point == "after_commit":
                raise RuntimeError("simulated crash")

        directory = self.control / self.tenant
        old = self.credential
        with self.assertRaises(RuntimeError):
            rotate_owner(
                self.pool, self.root, self.control, self.tenant, clock=self.clock, fault=crash
            )
        pending = directory / "owner.credential.next"
        self.assertTrue(pending.exists())
        self.assertEqual(old, (directory / "owner.credential").read_text())
        # The new credential is already the only one that works; the old file is stale.
        new = pending.read_text()
        self.assertEqual("credential_replaced", self.check(old, "admin", "*"))
        self.assertIsNone(self.check(new, "admin", "*"))
        rotate_owner(self.pool, self.root, self.control, self.tenant, clock=self.clock)
        self.assertFalse(pending.exists())
        self.assertEqual(new, (directory / "owner.credential").read_text())
        self.assertEqual(1, self.body(new)["generation"])  # completed, not rotated twice

    def test_a_stale_pending_file_is_discarded_and_rotation_proceeds(self):
        pending = self.control / self.tenant / "owner.credential.next"
        atomic(pending, b"not a credential")
        self.rotate()
        self.assertFalse(pending.exists())
        self.assertEqual(1, self.body(self.credential)["generation"])

    def test_rotation_needs_a_current_tenant_admin(self):
        with self.store() as store:
            reader = store.grant(self.credential, "reader", "demo", ["read"])
        path = self.control / self.tenant / "owner.credential"
        original = path.read_text()
        atomic(path, reader.encode())
        with self.assertRaises(LocalError) as caught:
            rotate_owner(self.pool, self.root, self.control, self.tenant, clock=self.clock)
        self.assertEqual("administrative_operation_required", caught.exception.code)
        self.assertEqual(reader, path.read_text())
        atomic(path, original.encode())
        self.assertIsNone(self.check(self.credential, "admin", "*"))

    def test_a_rotated_owner_survives_backup_and_restore(self):
        self.rotate()
        self.rotate()
        self.capture()
        archive, _ = self.backup_to("-rotated")
        self.credential = self.restore_from(archive).read_text()
        self.assertEqual(2, self.body(self.credential)["generation"])
        self.rotate()
        self.assertEqual(3, self.body(self.credential)["generation"])

    def test_the_shared_command_rotates_and_prints_no_credential(self):
        import contextlib
        import io

        from memoryking import shared

        dsn_file = self.base / (self.tenant + ".dsn")
        atomic(dsn_file, self.cluster.dsn("mk_runtime").encode())
        old = self.credential
        argv = [
            "rotate-owner",
            "--dsn-file",
            str(dsn_file),
            "--content",
            str(self.root),
            "--control",
            str(self.control),
            "--tenant",
            self.tenant,
        ]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = shared.run(shared.parser().parse_args(argv))
        self.assertEqual(
            {
                "credential_file": str(self.control / self.tenant / "owner.credential"),
                "rotated": True,
            },
            result,
        )
        self.credential = Path(result["credential_file"]).read_text()
        self.assertNotIn(self.credential, json.dumps(result) + out.getvalue())
        self.assertNotIn(old, json.dumps(result) + out.getvalue())
        self.assertEqual("credential_replaced", self.check(old, "admin", "*"))
        self.assertIsNone(self.check(self.credential, "admin", "*"))


if __name__ == "__main__":
    unittest.main()
