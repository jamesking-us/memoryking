"""Credential generations: revoke and rotation retire credentials, restore keeps them retired.

`CredentialTests` runs on SQLite here and on native PostgreSQL through
test_postgres_parity; the backend supplies `check` and the fixture attributes
(`store`, `owner`, `data`, `control`). Owner rotation is a local-profile offline command.
"""

import os
import tempfile
import time
import unittest
from pathlib import Path

from recovery_helpers import backup_fixture, restore_fixture

from memoryking.adapters.local_files import Control
from memoryking.adapters.local_store import LocalStore
from memoryking.domain.local import LocalError


class CredentialTests(unittest.TestCase):
    """Backend-neutral cases; a PostgreSQL fixture reuses `check`, `now_ms` and `pass_time`."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mk-cred-", dir="/tmp")
        base = Path(self.temp.name)
        self.root, self.data, self.control = base, base / "store", base / "control"
        self.owner = LocalStore.initialize(self.data, self.control).read_text()
        self.store = LocalStore(self.data, self.control)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def check(self, token, capability="read", project="demo"):
        """None when the credential authorizes the action, else the refusal code."""
        try:
            with self.store.transaction() as decision:
                self.store._auth(token, capability, project, decision.evaluated_ms)
        except LocalError as exc:
            return exc.code
        return None

    def now_ms(self):
        return int(time.time() * 1000)

    def pass_time(self, ms):
        time.sleep(ms / 1000)

    def issue(self, principal="agent", capabilities=("read",), project="demo", **extra):
        return self.store.grant(self.owner, principal, project, list(capabilities), **extra)

    def body(self, token):
        return self.store.control.verify(token)

    def test_revoke_then_regrant_yields_a_different_token_and_the_old_one_stays_refused(self):
        first = self.issue()
        self.assertIsNone(self.check(first))
        self.store.revoke(self.owner, "agent", "demo")
        self.assertEqual("denied", self.check(first))  # no grant: refused exactly as before
        second = self.issue(capabilities=("read", "capture"))
        self.assertNotEqual(first, second)
        self.assertIsNone(self.check(second, "capture"))
        self.assertEqual("credential_replaced", self.check(first))
        self.assertEqual(1, self.body(second)["generation"])
        # Revoking again and granting again keeps moving forward.
        self.store.revoke(self.owner, "agent", "demo")
        third = self.issue()
        self.assertEqual(2, self.body(third)["generation"])
        self.assertEqual("credential_replaced", self.check(second))
        self.assertIsNone(self.check(self.owner, "admin", "*"))

    def test_revoking_one_of_several_grants_keeps_the_credential_current(self):
        token = self.issue()
        self.assertEqual(token, self.issue(project="other"))
        self.store.revoke(self.owner, "agent", "other")
        self.assertIsNone(self.check(token))
        self.assertEqual("denied", self.check(token, project="other"))
        # Revoking something the principal never held changes nothing.
        self.store.revoke(self.owner, "agent", "never")
        self.assertIsNone(self.check(token))

    def test_reissue_keeps_the_token_and_rotation_replaces_it(self):
        first = self.issue()
        self.assertEqual(first, self.issue())
        self.assertNotIn("generation", self.body(first))
        rotated = self.issue(rotate=True)
        self.assertNotEqual(first, rotated)
        self.assertEqual(1, self.body(rotated)["generation"])
        self.assertEqual("credential_replaced", self.check(first))
        self.assertIsNone(self.check(rotated))
        self.assertEqual(rotated, self.issue())
        listed = {
            (item["principal"], item["project"]): item["capabilities"]
            for item in self.store.grants(self.owner)
        }
        self.assertEqual(["read"], listed[("agent", "demo")])
        again = self.issue(rotate=True)
        self.assertEqual(2, self.body(again)["generation"])
        self.assertEqual("credential_replaced", self.check(rotated))
        # Rotating one principal leaves every other credential alone.
        other = self.issue("another")
        self.issue(rotate=True)
        self.assertIsNone(self.check(other))
        self.assertIsNone(self.check(self.owner, "admin", "*"))

    def test_a_credential_issued_before_generations_works_until_rotated(self):
        token = self.issue()
        body = self.body(token)
        # The pre-change format is exactly the ungenerated body; nothing about it moved.
        self.assertEqual(
            {"kind", "store", "incarnation", "tenant", "principal"}, set(body) - {"audience"}
        )
        self.assertEqual(token, self.store.control.sign(body))
        self.assertIsNone(self.check(token))
        self.assertIsNone(self.check(self.owner, "admin", "*"))
        self.issue(rotate=True)
        self.assertEqual("credential_replaced", self.check(token))

    def test_a_generation_the_store_did_not_issue_is_refused(self):
        token = self.issue()
        body = self.body(token)
        for forged in (1, 7, -1, "1", True, False, None):
            signed = self.store.control.sign({**body, "generation": forged})
            self.assertEqual("credential_replaced", self.check(signed), forged)
        # Explicit zero is what a missing generation means.
        self.assertIsNone(self.check(self.store.control.sign({**body, "generation": 0})))

    def test_grant_expiry_is_enforced_against_the_store_clock(self):
        token = self.issue(expires=self.now_ms() + 400)
        self.assertIsNone(self.check(token))
        self.pass_time(600)
        self.assertEqual("denied", self.check(token))
        # The token itself stays current, so extending the grant revives it, and rotating retires it.
        self.assertEqual(token, self.issue(expires=self.now_ms() + 60_000))
        self.assertIsNone(self.check(token))
        self.issue(rotate=True)
        self.assertEqual("credential_replaced", self.check(token))

    def test_rotation_before_a_backup_and_after_it_both_survive_restore(self):
        agent = self.issue()
        agent = self.issue(rotate=True)
        gone = self.issue("gone")
        self.store.revoke(self.owner, "gone", "demo")
        archive = backup_fixture(self)
        self.issue(rotate=True)  # after the backup: only the journal knows
        self.store.revoke(self.owner, "agent", "demo")
        self.issue(capabilities=("read", "capture"))
        restore_fixture(self, archive)

        def generation(principal):
            with self.store.transaction():
                return self.store._generation(getattr(self, "tenant", "main"), principal)

        # Journal replay carried the later decisions into the restored database.
        self.assertEqual(3, generation("agent"))
        self.assertEqual(1, generation("gone"))
        # Every credential of the old history is retired by the new incarnation...
        for old in (agent, gone):
            self.assertEqual("history_changed", self.check(old))
        # ...and a token that only lacks the replayed generation is refused for that reason.
        fresh = self.issue(capabilities=("read",))
        body = self.body(fresh)
        self.assertEqual(3, body["generation"])
        self.assertIsNone(self.check(fresh))
        for behind in (0, 1, 2):
            stale = self.store.control.sign({**body, "generation": behind})
            self.assertEqual("credential_replaced", self.check(stale))
        revived = self.store.control.sign({k: v for k, v in body.items() if k != "generation"})
        self.assertEqual("credential_replaced", self.check(revived))
        # A principal that was revoked before the backup stays on its later generation.
        self.assertEqual(1, self.body(self.issue("gone"))["generation"])


class OwnerRotationTests(unittest.TestCase):
    """Offline rotation of the owner credential file (local profile)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mk-owner-", dir="/tmp")
        base = Path(self.temp.name)
        self.data, self.control = base / "store", base / "control"
        self.file = LocalStore.initialize(self.data, self.control)
        self.original = self.file.read_text()
        self.pending = self.control / "owner.credential.next"

    def tearDown(self):
        self.temp.cleanup()

    def check(self, token, capability="admin"):
        with LocalStore(self.data, self.control) as store:
            try:
                with store.transaction() as decision:
                    store._auth(token, capability, "*", decision.evaluated_ms)
            except LocalError as exc:
                return exc.code
        return None

    def test_rotation_replaces_the_file_and_retires_the_old_credential(self):
        path = LocalStore.rotate_owner(self.data, self.control)
        self.assertEqual(self.file, path)
        rotated = path.read_text()
        self.assertNotEqual(self.original, rotated)
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertFalse(self.pending.exists())
        self.assertEqual("credential_replaced", self.check(self.original))
        self.assertIsNone(self.check(rotated))
        self.assertEqual(1, Control(self.control).verify(rotated)["generation"])
        with LocalStore(self.data, self.control) as store:
            # The grant is exactly what initialisation made: every capability, no interval.
            row = store.db.execute("SELECT capabilities,starts,expires FROM grants").fetchone()
            self.assertIn("admin", row["capabilities"])
            self.assertEqual((None, None), (row["starts"], row["expires"]))
            self.assertEqual(
                "operator",
                store.db.execute("SELECT actor FROM events WHERE kind='grant'").fetchone()[0],
            )
        again = LocalStore.rotate_owner(self.data, self.control).read_text()
        self.assertEqual("credential_replaced", self.check(rotated))
        self.assertIsNone(self.check(again))

    def test_a_failure_after_the_commit_leaves_the_new_credential_in_the_pending_file(self):
        def crash(point):
            if point == "after_commit":
                raise OSError("simulated crash")

        with self.assertRaises(OSError):
            LocalStore.rotate_owner(self.data, self.control, fault=crash)
        # The old file is still in place but already retired; the new credential is on disk.
        self.assertEqual(self.original, self.file.read_text())
        self.assertTrue(self.pending.exists())
        pending = self.pending.read_text()
        self.assertEqual("credential_replaced", self.check(self.original))
        self.assertIsNone(self.check(pending))
        # Running the command again finishes the move instead of rotating twice.
        LocalStore.rotate_owner(self.data, self.control)
        self.assertEqual(pending, self.file.read_text())
        self.assertFalse(self.pending.exists())
        self.assertEqual(1, Control(self.control).verify(pending)["generation"])

    def test_a_stale_pending_file_is_discarded_and_rotation_proceeds(self):
        from memoryking.adapters.local_files import atomic

        atomic(self.pending, b"not a credential")
        LocalStore.rotate_owner(self.data, self.control)
        self.assertFalse(self.pending.exists())
        self.assertIsNone(self.check(self.file.read_text()))
        self.assertEqual("credential_replaced", self.check(self.original))

    def test_a_running_daemon_blocks_rotation_and_changes_nothing(self):
        with LocalStore(self.data, self.control):
            with self.assertRaises(LocalError) as caught:
                LocalStore.rotate_owner(self.data, self.control)
        self.assertEqual("owner_busy_or_unsafe", caught.exception.code)
        self.assertEqual(self.original, self.file.read_text())
        self.assertIsNone(self.check(self.original))

    def test_a_rotated_owner_survives_backup_and_restore(self):
        rotated = LocalStore.rotate_owner(self.data, self.control).read_text()
        archive = Path(self.temp.name) / "backup"
        with LocalStore(self.data, self.control) as store:
            store.backup(archive)
        restored = LocalStore.restore(self.data, self.control, archive).read_text()
        self.assertEqual("history_changed", self.check(rotated))
        self.assertEqual(1, Control(self.control).verify(restored)["generation"])
        self.assertIsNone(self.check(restored))
        # A credential that merely drops the generation does not revive the old one.
        body = Control(self.control).verify(restored)
        legacy = Control(self.control).sign({k: v for k, v in body.items() if k != "generation"})
        self.assertEqual("credential_replaced", self.check(legacy))

    def test_rotation_needs_a_current_tenant_admin(self):
        with LocalStore(self.data, self.control) as store:
            with store.transaction():
                store.db.execute("UPDATE grants SET capabilities='[\"read\"]'")
        with self.assertRaises(LocalError) as caught:
            LocalStore.rotate_owner(self.data, self.control)
        self.assertEqual("administrative_operation_required", caught.exception.code)
        self.assertEqual(self.original, self.file.read_text())


class CredentialMigrationTests(unittest.TestCase):
    def test_a_store_at_the_previous_schema_gains_the_table_and_keeps_its_credentials(self):
        with tempfile.TemporaryDirectory(prefix="mk-cred-mig-", dir="/tmp") as temp:
            base = Path(temp)
            owner = LocalStore.initialize(base / "s", base / "c").read_text()
            with LocalStore(base / "s", base / "c") as store:
                token = store.grant(owner, "agent", "demo", ["read"])
                store.db.execute("DROP TABLE credential_generations")
                store.db.execute("PRAGMA user_version=14")
            with LocalStore(base / "s", base / "c") as store:
                self.assertEqual(15, store.db.execute("PRAGMA user_version").fetchone()[0])
                self.assertEqual(
                    0, store.db.execute("SELECT count(*) FROM credential_generations").fetchone()[0]
                )
                with store.transaction() as decision:
                    store._auth(token, "read", "demo", decision.evaluated_ms)
                    store._auth(owner, "admin", "*", decision.evaluated_ms)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    unittest.main()
