"""M01 owner policy and viewer intersections on actual canonical transactions."""

import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext

import test_knowledge as knowledge
import test_postgres_parity as pg
from recovery_helpers import backup_fixture, restore_fixture
from test_local_store import FakeClock

from memoryking.adapters.local_ipc import StoreOperations
from memoryking.adapters.postgres_store import PostgresStore
from memoryking.adapters.retrieval import Retrieval
from memoryking.adapters.viewer_identity import ViewerIdentity
from memoryking.contracts.viewer import validate_viewer
from memoryking.domain.local import LocalError


class ViewerCases:
    def command(self, action, args, key=None):
        self.n += 1
        return {
            "version": 1,
            "incarnation": self.store.incarnation,
            "key": key or f"v{self.n}",
            "action": action,
            "args": args,
        }

    def policy(self, action, args, key=None):
        return ViewerIdentity(self.store).execute(self.owner, self.command(action, args, key))

    def bind(self, principal="phone", user_id="12345", key=None):
        return self.policy(
            "bind",
            {"principal": principal, "issuer": "https://github.com", "user_id": user_id},
            key,
        )

    def grant_viewer(self, version=1, project="p", **times):
        return self.policy(
            "grant",
            {
                "principal": "phone",
                "project": project,
                "expected_version": version,
                "starts": times.get("starts"),
                "expires": times.get("expires"),
            },
        )

    def subject(self, user_id="12345"):
        return ViewerIdentity(self.store).resolve_authenticated_subject(
            "https://github.com", user_id
        )

    def actor_store(self, token):
        return self.actor(token) if hasattr(self, "actor") else nullcontext(self.store)

    def read_viewer(self, subject, project="p", **options):
        with self.actor_store(subject.credential) as store:
            out = []
            store.read(subject.credential, project, **options).emit(out.append)
            return json.loads(out[0])

    def test_ac01_numeric_binding_rename_duplicate_and_denial(self):
        self.bind()
        self.grant_viewer()
        first = self.subject()
        # Login names are intentionally absent from authorization and cannot affect identity.
        renamed = self.subject()
        self.assertEqual(first.principal, renamed.principal)
        for issuer, uid in (
            ("https://enterprise.invalid", "12345"),
            ("https://github.com", "54321"),
        ):
            with self.assertRaises(LocalError):
                ViewerIdentity(self.store).resolve_authenticated_subject(issuer, uid)
        with self.assertRaises(LocalError):
            self.bind("other", "12345")
        with self.assertRaises(LocalError):
            self.bind("owner", "54321")
        for uid in ("0123", "", "-1", "not-a-number"):
            with self.assertRaises(LocalError):
                self.bind("other", uid)

    def test_ac02_project_grant_never_shares_owner_only_content(self):
        self.bind()
        self.grant_viewer()
        subject = self.subject()
        public = self.read_viewer(subject)
        self.assertIn(self.obs["observation"], json.dumps(public))
        private = self.store.capture(
            self.owner,
            self.ns,
            "private",
            {
                "version": 1,
                "project": "p",
                "source": "test",
                "source_event": "private",
                "text": "OWNER_ONLY_MARKER",
                "readers": ["owner"],
                "classification": "restricted",
                "observed_at_ms": 1,
            },
        )
        packet = self.read_viewer(subject)
        self.assertNotIn("OWNER_ONLY_MARKER", json.dumps(packet))
        for options in ({"observation": private["handle"]}, {"export": True}):
            with self.assertRaises(LocalError):
                self.read_viewer(subject, **options)
        with self.assertRaises(LocalError):
            self.read_viewer(subject, "other")
        with self.actor_store(subject.credential) as store:
            out = []
            Retrieval(store).execute(
                subject.credential,
                {
                    "version": 1,
                    "incarnation": store.incarnation,
                    "action": "atlas",
                    "args": {"project": "p", "bindings": [{}]},
                },
            ).emit(out.append)
            self.assertNotIn("OWNER_ONLY_MARKER", out[0].decode())

    def test_ac03_retries_ceiling_logout_and_binding_changes(self):
        result = self.bind(key="stable")
        repeat = ViewerIdentity(self.store).execute(
            self.owner,
            self.command(
                "bind",
                {"principal": "phone", "issuer": "https://github.com", "user_id": "12345"},
                "stable",
            ),
        )
        self.assertEqual(result, repeat)
        with self.assertRaises(LocalError) as mismatch:
            self.bind(user_id="22222", key="stable")
        self.assertEqual(mismatch.exception.code, "idempotency_conflict")
        self.grant_viewer()
        old = self.subject()
        self.policy("logout_all", {"principal": "phone", "expected_version": 2})
        with self.assertRaises(LocalError):
            self.read_viewer(old)
        self.assertTrue(self.read_viewer(self.subject())["items"])
        with self.assertRaises(LocalError):
            self.store.grant(self.owner, "phone", "p", ["capture"])
        with self.actor_store(self.subject().credential) as store:
            viewer = self.subject()
            with self.assertRaises(LocalError):
                ViewerIdentity(store).execute(viewer.credential, self.command("list", {}))
        self.policy("rebind", {"principal": "phone", "expected_version": 3, "user_id": "55555"})
        with self.assertRaises(LocalError):
            self.subject()
        # Rebinding never silently transfers old project grants to another account.
        with self.assertRaises(LocalError):
            self.read_viewer(self.subject("55555"))

    def test_ac03_concurrent_expected_version_only_one_wins(self):
        self.bind()
        barrier = threading.Barrier(2)
        commands = [
            self.command(
                "grant",
                {
                    "principal": "phone",
                    "project": p,
                    "expected_version": 1,
                    "starts": None,
                    "expires": None,
                },
            )
            for p in ("p", "q")
        ]

        def run(command):
            barrier.wait()
            try:
                if isinstance(self.store, PostgresStore):
                    with PostgresStore(
                        self.pool, self.data, self.control, self.tenant, self.owner
                    ) as store:
                        ViewerIdentity(store).execute(self.owner, command)
                else:
                    ViewerIdentity(self.store).execute(self.owner, command)
                return "committed"
            except LocalError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, commands))
        self.assertCountEqual(results, ["committed", "revision_conflict"])

    def test_ac03_after_journal_crash_replays_one_policy_result(self):
        command = self.command(
            "bind", {"principal": "phone", "issuer": "https://github.com", "user_id": "12345"}
        )

        def fail(point):
            if point == "after_journal":
                raise RuntimeError("synthetic crash before policy projection")

        self.store.fault = fail
        with self.assertRaises(RuntimeError):
            ViewerIdentity(self.store).execute(self.owner, command)
        self.store.fault = lambda _: None
        self.store.close()
        if isinstance(self.store, PostgresStore):
            from test_postgres_parity import InspectionSQL

            self.store = PostgresStore(self.pool, self.data, self.control, self.tenant, self.owner)
            self.store.db = InspectionSQL(self.store, self.store.db)
        else:
            from memoryking.adapters.local_store import LocalStore

            self.store = LocalStore(self.data, self.control)
        result = ViewerIdentity(self.store).execute(self.owner, command)
        self.assertEqual(result["version"], 1)
        self.assertEqual(self.subject().principal, "phone")

    def test_ac03_disable_and_content_grant_cannot_be_bypassed(self):
        self.bind()
        self.grant_viewer()
        subject = self.subject()
        self.policy("disable", {"principal": "phone", "expected_version": 2})
        with self.assertRaises(LocalError):
            self.subject()
        with self.assertRaises(LocalError):
            self.read_viewer(subject)
        with self.assertRaises(LocalError):
            self.grant_viewer(version=3)

    def test_ac02_viewer_cannot_select_or_write_another_tenant(self):
        self.bind()
        self.grant_viewer()
        subject = self.subject()
        with self.actor_store(subject.credential) as store:
            forged = store._token(
                "credential",
                tenant="unrelated",
                principal=subject.principal,
                viewer={
                    "user_id": subject.user_id,
                    "generation": subject.generation,
                    "policy_epoch": subject.policy_epoch,
                },
            )
            with self.assertRaises(LocalError):
                store.read(forged, "p")
            with self.assertRaises(LocalError):
                store.namespace(subject.credential, "p")

    def test_ac02_task_projection_counts_only_visible_checkpoints(self):
        from test_working import checkpoint

        from memoryking.adapters.execution import Execution
        from memoryking.adapters.working import Working

        self.bind()
        self.grant_viewer()
        for task, readers in (("visible", ["owner", "phone"]), ("OWNER_ONLY_TASK", ["owner"])):
            body = checkpoint()
            body["objective"] = task
            out = []
            Working(self.store).execute(
                self.owner,
                {
                    "version": 1,
                    "incarnation": self.store.incarnation,
                    "key": task,
                    "action": "save",
                    "args": {
                        "project": "p",
                        "task": task,
                        "run": None,
                        "expected_revision": 0,
                        "readers": readers,
                        "checkpoint": body,
                    },
                },
            ).emit(out.append)
        subject = self.subject()
        with self.actor_store(subject.credential) as store:
            with store.transaction() as decision:
                who = store._auth(subject.credential, "read", "p", decision.evaluated_ms)
                view = Execution(store).snapshot(who, "p", decision.evaluated_ms)
        self.assertEqual(len(view["tasks"]), 1)
        self.assertNotIn("OWNER_ONLY_TASK", json.dumps(view))
        self.assertEqual(view["tasks"][0]["progress"]["total"], 1)

    def test_ac04_real_restore_preserves_current_revoke_and_history_fences(self):
        self.bind()
        self.grant_viewer()
        old = self.subject()
        old_position = self.store.status(self.owner, "p")["position"]
        old_watermark = self.store._token("watermark", tenant=self.tenant, position=old_position)
        old_cursor = self.read_viewer(old)["cursor"]
        archive = backup_fixture(self)
        self.policy("revoke", {"principal": "phone", "project": "p", "expected_version": 2})
        old_command = self.command("list", {})
        restore_fixture(self, archive)
        # A replacement history advancing past old numeric positions cannot rehabilitate handles.
        namespace = self.store.namespace(self.owner, "p")
        for i in range(old_position + 2):
            self.store.capture(
                self.owner,
                namespace,
                f"after{i}",
                {
                    "version": 1,
                    "project": "p",
                    "source": "restore-fixture",
                    "source_event": f"after{i}",
                    "text": "replacement history",
                    "readers": ["owner"],
                    "classification": "internal",
                    "observed_at_ms": 1,
                },
            )
        with self.assertRaises(LocalError):
            self.store.status(self.owner, "p", watermark=old_watermark)
        with self.assertRaises(LocalError) as cursor_error:
            self.store.read(self.owner, "p", cursor=old_cursor)
        self.assertEqual(cursor_error.exception.code, "history_changed")
        with self.assertRaises(LocalError):
            self.read_viewer(old)
        with self.assertRaises(LocalError) as history:
            ViewerIdentity(self.store).execute(self.owner, old_command)
        self.assertEqual(history.exception.code, "history_changed")
        with self.assertRaises(LocalError):
            self.read_viewer(self.subject())

    def test_ac05_persisted_evaluation_instant_and_release_deadline(self):
        clock = FakeClock()
        # Stay after existing durable UTC evidence when replacing the fixture clock.
        clock.utc = max(clock.utc, self.store.last_utc + 10000)
        self.store.clock = clock
        self.bind()
        self.grant_viewer(expires=clock.utc + 1000)
        subject = self.subject()

        def stall(point):
            if point == "after_validation":
                clock.advance(2000)

        self.store.fault = stall
        with self.actor_store(subject.credential) as store:
            packet = store.read(subject.credential, "p")
            output = []
            packet.emit(output.append)
            self.assertTrue(output)
        self.store.fault = lambda _: None
        with self.assertRaises(LocalError):
            self.read_viewer(subject)
        self.grant_viewer(version=2, expires=None)
        renewed = self.subject()
        with self.actor_store(renewed.credential) as store:
            late = store.read(renewed.credential, "p")
            clock.advance(5001)
            with self.assertRaises(LocalError):
                late.emit(lambda _: self.fail("late content emitted"))

    def test_owner_wire_closed_contracts_and_no_mcp_discovery(self):
        from memoryking.sdk import CATALOG

        command = self.command(
            "bind", {"principal": "phone", "issuer": "https://github.com", "user_id": "12345"}
        )
        output = StoreOperations(self.store).execute(
            {
                "version": 1,
                "credential": self.owner,
                "operation": "viewer_admin",
                "args": {"command": command},
            }
        )
        self.assertEqual(output["principal"], "phone")
        self.assertNotIn("viewer_admin", json.dumps(CATALOG))
        with self.assertRaises(LocalError):
            validate_viewer("ViewerAdminCommand", {**command, "unchecked": True})
        with self.assertRaises(LocalError):
            validate_viewer("ReportPolicy", {"version": 1}, reporting=True)


class LocalViewerIdentity(ViewerCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown


class PostgresViewerIdentity(ViewerCases, pg.NativeFixture):
    pass


if __name__ == "__main__":
    unittest.main()
