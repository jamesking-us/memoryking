"""Delegated read authorization and lifecycle, on both actual database backends."""

import base64
import concurrent.futures
import hashlib
import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator
from test_local_store import FakeClock, delivered, observation

from memoryking.adapters.local_ipc import StoreOperations
from memoryking.adapters.local_store import LocalStore
from memoryking.adapters.platform_support import unix_stream_socket
from memoryking.application.retention import release_receipts
from memoryking.application.viewer import AUDIENCE, READ_KEYS, Viewer
from memoryking.contracts.canonical import canonical_bytes, canonical_hash
from memoryking.domain.local import LocalError

SESSION = "a" * 64


class ViewerCases:
    def test_native_owner_capabilities_remain_unchanged(self):
        native = {
            "capture",
            "read",
            "export",
            "jobs",
            "admin",
            "review",
            "policy",
            "attest",
            "task_constraint",
            "checkpoint",
            "handoff",
            "inference",
            "repository",
        }
        with self.open() as store:
            context = delivered(store.context(self.owner, "demo"))
            self.assertEqual(native, set(context["capabilities"]))
            with store.transaction():
                grant = store.db.execute(
                    "SELECT capabilities FROM grants WHERE tenant=? AND principal='owner' AND project='*'",
                    (self.tenant,),
                ).fetchone()
                self.assertEqual(native, set(json.loads(grant[0])))

    def setup_viewer(self):
        self.clock = FakeClock()
        with self.open() as store:
            self.namespace = store.namespace(self.owner, "demo")
            self.backend = store.grant(self.owner, "backend", "demo", ["viewer_exchange"])
            self.subject = store.grant(self.owner, "subject", "demo", ["read"])
            self.registry = dict(
                installation_id="test",
                provider="github.com",
                delegator="backend",
                project="demo",
                audience=AUDIENCE,
                starts=None,
                expires=None,
                enabled=True,
            )
            self.mapping = dict(
                installation_id="test",
                provider="github.com",
                github_id="123",
                subject="subject",
                starts=None,
                expires=None,
                enabled=True,
            )
            Viewer(store).configure(self.owner, "registry", self.registry)
            Viewer(store).configure(self.owner, "mapping", self.mapping)
        self.exchange_args = dict(
            installation_id="test",
            audience=AUDIENCE,
            project="demo",
            provider="github.com",
            github_id="123",
            session_ref=SESSION,
        )

    def invoke(self, operation, args, credential=None):
        credential = credential or self.backend
        with self.open(credential, release=True) as store:
            result = delivered(
                StoreOperations(store).execute(
                    dict(version=1, operation=operation, args=args, credential=credential)
                )
            )
            schema = json.loads(
                (
                    Path(__file__).resolve().parents[1] / "contracts/viewer-v1.schema.json"
                ).read_text()
            )
            response = {
                "viewer_exchange": "ExchangeResponse",
                "viewer_read": "ReadResponse",
                "viewer_close": "CloseResponse",
            }[operation]
            Draft202012Validator(
                {"$defs": schema["$defs"], "$ref": "#/$defs/" + response}
            ).validate(result)
            return result

    def exchange(self, **changes):
        return self.invoke("viewer_exchange", {**self.exchange_args, **changes})

    def read(self, lease, **changes):
        return self.invoke(
            "viewer_read",
            {**dict(lease_id=lease, session_ref=SESSION, query="", cursor=""), **changes},
        )

    def close_lease(self, lease):
        return self.invoke("viewer_close", dict(lease_id=lease, session_ref=SESSION))

    def capture(self, n, **changes):
        with self.open() as store:
            return store.capture(self.owner, self.namespace, f"k{n}", observation(n, **changes))

    def refused(self, code, fn, *args, **kwargs):
        with self.assertRaises(LocalError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)
        self.assertEqual({}, caught.exception.detail)

    def test_subject_visibility_receipt_and_native_separation(self):
        self.capture(1, readers=["subject"])
        self.capture(2, readers=["owner"])
        packet = self.exchange()
        self.assertEqual(30000, packet["valid_for_ms"])
        self.assertEqual(43, len(packet["lease_id"]))
        view = self.read(packet["lease_id"])
        self.assertEqual(1, len(view["data"]["items"]))
        self.assertEqual(set(READ_KEYS), set(view["data"]["items"][0]))
        self.assertEqual(5000, view["release_for_ms"])
        self.assertEqual(15000, view["display_for_ms"])
        with self.open() as store:
            with store.transaction():
                digest = hashlib.sha256(packet["lease_id"].encode()).hexdigest()
                row = store.db.execute(
                    "SELECT body FROM viewer_leases WHERE tenant=? AND lease_hash=?",
                    (self.tenant, digest),
                ).fetchone()
                self.assertNotIn(packet["lease_id"], row[0])
                receipt = store.db.execute(
                    "SELECT principal,response_hash FROM receipts WHERE tenant=? AND id=?",
                    (self.tenant, view["access_receipt"]),
                ).fetchone()
                self.assertEqual(("subject", canonical_hash(view)), tuple(receipt))
                context = store.db.execute(
                    "SELECT context FROM viewer_receipt_context WHERE tenant=? AND receipt=?",
                    (self.tenant, view["access_receipt"]),
                ).fetchone()[0]
                self.assertEqual("backend", json.loads(context)["delegator"])
        with self.open(self.backend, release=True) as store:
            with self.assertRaises(LocalError):
                store.read(self.backend, "demo")
        with self.open(self.subject, release=True) as store:
            native = delivered(store.read(self.subject, "demo"))
            self.assertIn("handle", native["items"][0])

    def test_close_idempotent_session_and_expiry(self):
        lease = self.exchange()["lease_id"]
        self.refused("lease_invalid", self.read, lease, session_ref="b" * 64)
        self.assertEqual({"version": 1, "closed": True}, self.close_lease(lease))
        self.assertEqual({"version": 1, "closed": True}, self.close_lease(lease))
        self.refused("lease_invalid", self.read, lease)
        other = self.exchange()["lease_id"]
        self.clock.advance(30000)
        self.refused("lease_expired", self.read, other)
        self.refused("lease_invalid", self.read, other, cursor="malformed")

    def test_journaled_mapping_survives_interrupted_apply(self):
        with self.open() as store:
            old_fault = store.fault

            def fault(stage):
                if stage == "after_journal":
                    raise RuntimeError("injected journal failure")

            store.fault = fault
            try:
                with self.assertRaises(RuntimeError):
                    Viewer(store).configure(
                        self.owner, "mapping", {**self.mapping, "enabled": False}
                    )
            finally:
                store.fault = old_fault
        self.restart()
        with self.open() as store:
            with store.transaction():
                row = store.db.execute(
                    "SELECT revision,enabled FROM viewer_mappings WHERE tenant=?", (self.tenant,)
                ).fetchone()
                self.assertEqual((2, 0), tuple(row))
        self.refused("denied", self.exchange)

    def test_backend_and_subject_cannot_overlap_or_escalate(self):
        self.refused("denied", self.invoke, "viewer_exchange", self.exchange_args, self.owner)
        with self.open() as store:
            with self.assertRaises(LocalError):
                Viewer(store).configure(
                    self.owner, "mapping", {**self.mapping, "subject": "backend"}
                )
            store.grant(self.owner, "backend", "other", ["read"])
        self.refused("denied", self.exchange)

    def test_current_generations_mapping_revision_and_explicit_grant(self):
        lease = self.exchange()["lease_id"]
        with self.open() as store:
            store.grant(self.owner, "subject", "demo", ["read"], rotate=True)
        self.refused("lease_invalid", self.read, lease)
        lease = self.exchange()["lease_id"]
        with self.open() as store:
            Viewer(store).configure(self.owner, "mapping", self.mapping)
        self.refused("lease_invalid", self.read, lease)
        with self.open() as store:
            with store.transaction() as decision:
                store._apply(
                    store._journal(
                        dict(
                            kind="grant",
                            tenant=self.tenant,
                            actor="owner",
                            principal="subject",
                            project="*",
                            capabilities=["read"],
                            starts=None,
                            expires=None,
                        )
                    ),
                    decision.evaluated_ms,
                )
            store.revoke(self.owner, "subject", "demo")
        self.refused("denied", self.exchange)

    def test_candidate_cursor_privacy_query_and_native_cursor_refusal(self):
        for i in range(1, 34):
            self.capture(i, readers=["owner"] if i < 33 else ["subject"])
        lease = self.exchange()["lease_id"]
        first = self.read(lease)
        self.assertEqual([], first["data"]["items"])
        self.assertTrue(first["cursor"])
        body = first["cursor"].split(".")[0]
        cursor = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        self.assertEqual(
            {
                "kind",
                "store",
                "incarnation",
                "lease_hash",
                "context_sha256",
                "query_sha256",
                "position",
            },
            set(cursor),
        )
        self.assertNotIn(SESSION, json.dumps(cursor))
        self.refused("lease_invalid", self.read, lease, cursor=first["cursor"], query="different")
        second = self.read(lease, cursor=first["cursor"])
        self.assertEqual(1, len(second["data"]["items"]))
        self.assertEqual("", second["cursor"])
        other = self.exchange()["lease_id"]
        self.refused("lease_invalid", self.read, other, cursor=first["cursor"])
        with self.open(self.subject, release=True) as store:
            native = delivered(store.read(self.subject, "demo"))["cursor"]
        self.refused("lease_invalid", self.read, lease, cursor=native)

    def test_ttl_shortening_and_pre_byte_deadline(self):
        with self.open() as store:
            store.grant(self.owner, "subject", "demo", ["read"], expires=self.clock.utc + 100)
        packet = self.exchange()
        self.assertEqual(100, packet["valid_for_ms"])
        with self.open(self.backend, release=True) as store:
            release = Viewer(store).execute(
                self.backend,
                "viewer_read",
                dict(lease_id=packet["lease_id"], session_ref=SESSION, query="", cursor=""),
            )
            self.clock.advance(100)
            emitted = []
            self.refused("release_expired", release.emit, emitted.append)
            self.assertEqual([], emitted)

    def test_oversize_continuation_and_receipt_purge(self):
        first = self.capture(1)
        self.capture(2)
        # Simulate oversized retained legacy metadata (capture has tighter current bounds).
        with self.open() as store:
            with store.transaction():
                store.db.execute(
                    "UPDATE observations SET source=? WHERE tenant=? AND id=?",
                    ("s" * 16385, self.tenant, first["observation"]),
                )
        lease = self.exchange()["lease_id"]
        page = self.read(lease)
        self.assertEqual([], page["data"]["items"])
        self.assertEqual("item_bounds_exceeded", page["oversize"][0]["code"])
        following = self.read(lease, cursor=page["cursor"])
        self.assertEqual(1, len(following["data"]["items"]))
        with self.open() as store:
            item = following["data"]["items"][0]
            store.purge(self.owner, item["id"])
            with store.transaction():
                self.assertIsNone(
                    store.db.execute(
                        "SELECT 1 FROM viewer_receipt_context WHERE tenant=? AND receipt=?",
                        (self.tenant, following["access_receipt"]),
                    ).fetchone()
                )

    def test_closed_shape_capacity_and_bounded_cleanup(self):
        self.refused("input_invalid", self.exchange, principal="owner")
        self.refused("input_invalid", self.exchange, github_id="0123")
        for _ in range(16):
            self.exchange()
        self.refused("service_busy", self.exchange)
        self.clock.advance(30000)
        self.assertTrue(self.exchange()["lease_id"])

    def test_policy_revocation_ineligible_subject_and_owned_close(self):
        lease = self.exchange()["lease_id"]
        with self.open() as store:
            Viewer(store).configure(self.owner, "mapping", {**self.mapping, "enabled": False})
        self.refused("lease_invalid", self.read, lease)
        self.assertTrue(self.close_lease(lease)["closed"])
        with self.open() as store:
            with self.assertRaises(LocalError):
                Viewer(store).configure(
                    self.owner, "registry", {**self.registry, "delegator": "subject"}
                )
            Viewer(store).configure(self.owner, "mapping", self.mapping)
            store.grant(self.owner, "subject", "other", ["viewer_exchange"])
        self.refused("denied", self.exchange)

    def test_backend_rotation_and_foreign_lease(self):
        lease = self.exchange()["lease_id"]
        with self.open() as store:
            alternate = store.grant(self.owner, "alternate", "demo", ["viewer_exchange"])
            Viewer(store).configure(
                self.owner, "registry", {**self.registry, "delegator": "alternate"}
            )
        self.refused(
            "lease_invalid",
            self.invoke,
            "viewer_close",
            dict(lease_id=lease, session_ref=SESSION),
            alternate,
        )
        with self.open() as store:
            self.backend = store.grant(
                self.owner, "backend", "demo", ["viewer_exchange"], rotate=True
            )
        self.refused("lease_invalid", self.read, lease)

    def test_cleanup_is_bounded_and_does_not_remove_receipt_context(self):
        self.capture(1)
        lease = self.exchange()["lease_id"]
        receipt = self.read(lease)["access_receipt"]
        self.clock.advance(30000)
        with self.open() as store:
            with store.transaction():
                for n in range(130):
                    store.db.execute(
                        "INSERT INTO viewer_leases(tenant,lease_hash,delegator,subject,session_ref,expires,closed,body) VALUES(?,?,?,?,?,?,0,?)",
                        (
                            self.tenant,
                            f"{n:064x}",
                            "backend",
                            "subject",
                            SESSION,
                            self.clock.utc - 1,
                            "{}",
                        ),
                    )
        self.exchange()
        with self.open() as store:
            with store.transaction():
                count = store.db.execute(
                    "SELECT count(*) FROM viewer_leases WHERE tenant=?", (self.tenant,)
                ).fetchone()[0]
                self.assertEqual(68, count)  # 131 old rows - 64 + one current row
                self.assertIsNotNone(
                    store.db.execute(
                        "SELECT 1 FROM viewer_receipt_context WHERE tenant=? AND receipt=?",
                        (self.tenant, receipt),
                    ).fetchone()
                )
                release_receipts(store.db, self.tenant, self.clock.utc)
                self.assertIsNone(
                    store.db.execute(
                        "SELECT 1 FROM viewer_receipt_context WHERE tenant=? AND receipt=?",
                        (self.tenant, receipt),
                    ).fetchone()
                )

    def test_escape_heavy_text_pages_do_not_consume_unemitted_item(self):
        for n in range(1, 5):
            self.capture(n, text='"' * 65536)
        lease = self.exchange()["lease_id"]
        page = self.read(lease)
        self.assertEqual(1, len(page["data"]["items"]))
        self.assertLessEqual(len(json.dumps(page).encode()), 262144)
        second = self.read(lease, cursor=page["cursor"])
        self.assertEqual(1, len(second["data"]["items"]))
        self.assertNotEqual(page["data"]["items"][0]["id"], second["data"]["items"][0]["id"])

    def test_backend_and_tenant_live_capacity_cannot_be_bypassed(self):
        for total, delegator in [(256, "backend"), (1024, "other")]:
            with self.open() as store:
                with store.transaction():
                    store.db.execute("DELETE FROM viewer_leases WHERE tenant=?", (self.tenant,))
                    for n in range(total):
                        store.db.execute(
                            "INSERT INTO viewer_leases(tenant,lease_hash,delegator,subject,session_ref,expires,closed,body) VALUES(?,?,?,?,?,?,0,?)",
                            (
                                self.tenant,
                                f"{n:064x}",
                                delegator,
                                "other",
                                "b" * 64,
                                self.clock.utc + 30000,
                                "{}",
                            ),
                        )
            self.refused("service_busy", self.exchange)

    def test_shape_precedence_and_untrusted_clock(self):
        self.refused(
            "denied",
            self.invoke,
            "viewer_exchange",
            {**self.exchange_args, "principal": "owner"},
            self.owner,
        )
        lease = self.exchange()["lease_id"]
        with self.open(self.backend, release=True) as store:
            release = Viewer(store).execute(
                self.backend,
                "viewer_read",
                dict(lease_id=lease, session_ref=SESSION, query="", cursor=""),
            )
        self.clock.mono -= 1
        self.refused("clock_untrusted", release.emit, lambda _: None)


class SQLiteViewerTests(ViewerCases, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mk-viewer-", dir="/tmp")
        root = Path(self.temp.name)
        self.root, self.control = root / "db", root / "ctl"
        self.owner = LocalStore.initialize(self.root, self.control).read_text()
        self.tenant = "main"
        self.store = None
        self.setup_viewer()

    @contextmanager
    def open(self, credential=None, release=False):
        if self.store is None:
            self.store = LocalStore(self.root, self.control, clock=self.clock)
        yield self.store

    def test_unix_socket_private_operations(self):
        from memoryking.adapters.local_ipc import Server, request

        with Server(self.store) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                lease = request(server.path, self.backend, "viewer_exchange", self.exchange_args)
                read = request(
                    server.path,
                    self.backend,
                    "viewer_read",
                    dict(lease_id=lease["lease_id"], session_ref=SESSION, query="", cursor=""),
                )
                self.assertEqual([], read["data"]["items"])
                self.assertTrue(
                    request(
                        server.path,
                        self.backend,
                        "viewer_close",
                        dict(lease_id=lease["lease_id"], session_ref=SESSION),
                    )["closed"]
                )
                with unix_stream_socket() as stream:
                    stream.connect(str(server.path))
                    stream.sendall(
                        b" " * 16384
                        + canonical_bytes(
                            dict(
                                version=1,
                                operation="viewer_read",
                                credential=self.backend,
                                args=dict(
                                    lease_id=lease["lease_id"],
                                    session_ref=SESSION,
                                    query="",
                                    cursor="",
                                ),
                            )
                        )
                        + b"\n"
                    )
                    with stream.makefile("rb") as reply:
                        refusal = json.loads(reply.readline(1024))
                    self.assertEqual("bounds_exceeded", refusal["error"])
                # An operation that is a JSON list or object is a malformed request, as it was
                # before these operations existed, never an internal error.
                for operation in ([], {}, ["viewer_read"]):
                    with unix_stream_socket() as stream:
                        stream.connect(str(server.path))
                        stream.sendall(
                            canonical_bytes(
                                dict(
                                    version=1,
                                    operation=operation,
                                    credential=self.backend,
                                    args={},
                                )
                            )
                            + b"\n"
                        )
                        with stream.makefile("rb") as reply:
                            refusal = json.loads(reply.readline(4096))
                    self.assertEqual("invalid_request", refusal["error"], operation)
            finally:
                server.shutdown()
                thread.join(5)

    def restart(self):
        self.store.close()
        self.store = None

    def test_restore_retires_leases_and_retains_journaled_configuration(self):
        lease = self.exchange()["lease_id"]
        archive = Path(self.temp.name) / "backup"
        self.store.backup(archive)
        self.restart()
        LocalStore.restore(self.root, self.control, archive, clock=self.clock)
        self.owner = (self.control / "owner.credential").read_text()
        self.refused("history_changed", self.read, lease)
        with self.open() as store:
            with store.transaction():
                self.assertEqual(
                    0, store.db.execute("SELECT count(*) FROM viewer_leases").fetchone()[0]
                )
                self.assertEqual(
                    1, store.db.execute("SELECT count(*) FROM viewer_mappings").fetchone()[0]
                )

    def tearDown(self):
        if self.store:
            self.store.close()
        self.temp.cleanup()


BIN = Path(os.environ.get("MEMORYKING_PG_BIN", "tmp/postgres-dev/runtime/bin")).resolve()


@unittest.skipUnless((BIN / "pg_ctl").is_file(), "native PostgreSQL runtime not configured")
class PostgreSQLViewerTests(ViewerCases, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_postgres_store import PostgresStoreTests

        PostgresStoreTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        from test_postgres_store import PostgresStoreTests

        PostgresStoreTests.tearDownClass.__func__(cls)

    def setUp(self):
        import uuid

        import psycopg

        from memoryking.adapters.postgres_admin import initialize_tenant

        self.tenant = "t_" + uuid.uuid4().hex
        self.root, self.control = self.base / "content", self.base / "control"
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as connection:
            self.owner = initialize_tenant(
                connection, self.root, self.control, self.tenant
            ).read_text()
        self.setup_viewer()

    def restart(self):
        pass  # Every PostgreSQL operation already opens a new request-bound adapter.

    def test_migration_is_idempotent_and_restore_retires_leases(self):
        import psycopg

        from memoryking.adapters.postgres_admin import migrate
        from memoryking.adapters.postgres_recovery import backup, restore

        lease = self.exchange()["lease_id"]
        archive = self.base / (self.tenant + "-backup")
        with (
            self.open() as store,
            psycopg.connect(self.cluster.dsn("mk_backup"), autocommit=True) as connection,
        ):
            backup(store, connection, archive)
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as connection:
            migrate(connection)
            self.owner = restore(
                connection, self.root, self.control, self.tenant, archive, clock=self.clock
            ).read_text()
        self.refused("history_changed", self.read, lease)
        with self.open() as store:
            with store.transaction():
                self.assertEqual(
                    0,
                    store.db.execute(
                        "SELECT count(*) FROM viewer_leases WHERE tenant=?", (self.tenant,)
                    ).fetchone()[0],
                )
                self.assertEqual(
                    1,
                    store.db.execute(
                        "SELECT count(*) FROM viewer_mappings WHERE tenant=?", (self.tenant,)
                    ).fetchone()[0],
                )

    def open(self, credential=None, release=False, append=False):
        from memoryking.adapters.postgres_store import PostgresStore

        return PostgresStore(
            self.pool,
            self.root,
            self.control,
            self.tenant,
            credential or self.owner,
            clock=self.clock,
            release_only=release,
            append=append,
        )

    def test_concurrent_capture_does_not_abort_viewer_receipt(self):
        self.capture(1, readers=["subject"])
        lease = self.exchange()["lease_id"]
        entered, resume = threading.Event(), threading.Event()

        def reading():
            with self.open(self.backend, release=True) as store:

                def fault(stage):
                    if stage == "after_validation":
                        entered.set()
                        if not resume.wait(3):
                            raise AssertionError("capture barrier timed out")

                store.fault = fault
                return delivered(
                    Viewer(store).execute(
                        self.backend,
                        "viewer_read",
                        dict(lease_id=lease, session_ref=SESSION, query="", cursor=""),
                    )
                )

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(reading)
            self.assertTrue(entered.wait(3))
            try:
                with self.open(append=True) as store:
                    store.capture(self.owner, self.namespace, "concurrent", observation(2))
            finally:
                resume.set()
            self.assertEqual(1, len(future.result(timeout=5)["data"]["items"]))

    def test_read_queued_behind_revocation_evaluates_current_authority(self):
        from memoryking.adapters.postgres_store import take_gate

        lease = self.exchange()["lease_id"]
        revoking, resume, queued = threading.Event(), threading.Event(), threading.Event()

        def revoke():
            with self.open() as store:

                def fault(stage):
                    if stage == "after_journal":
                        revoking.set()
                        if not resume.wait(3):
                            raise AssertionError("revocation barrier timed out")

                store.fault = fault
                store.revoke(self.owner, "subject", "demo")

        def gate(connection, tenant, *, shared):
            if shared and tenant == self.tenant:
                queued.set()
            return take_gate(connection, tenant, shared=shared)

        # Construct the reader before the revoker takes the control/journal lock.
        with self.open(self.backend, release=True) as reader:

            def read():
                return delivered(
                    Viewer(reader).execute(
                        self.backend,
                        "viewer_read",
                        dict(lease_id=lease, session_ref=SESSION, query="", cursor=""),
                    )
                )

            with patch("memoryking.adapters.postgres_store.take_gate", gate):
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                    writer = executor.submit(revoke)
                    self.assertTrue(revoking.wait(3))
                    result = executor.submit(read)
                    try:
                        self.assertTrue(queued.wait(3))
                        self.assertFalse(result.done())
                    finally:
                        resume.set()
                    writer.result(timeout=5)
                    with self.assertRaises(LocalError) as caught:
                        result.result(timeout=5)
                    self.assertIn(caught.exception.code, {"lease_invalid", "denied"})

    def test_native_delegate_refuses_exchange_only_capability(self):
        with self.open() as store:
            with self.assertRaises(LocalError) as caught:
                store.delegate("subject", "demo", ["viewer_exchange"], 1000)
            self.assertEqual("invalid_delegation", caught.exception.code)

    def test_read_holds_lease_against_close_and_parallel_admission_is_bounded(self):
        lease = self.exchange()["lease_id"]
        entered, resume = threading.Event(), threading.Event()

        def reading():
            with self.open(self.backend, release=True) as store:

                def fault(stage):
                    if stage == "after_validation":
                        entered.set()
                        if not resume.wait(2):
                            raise AssertionError("test release barrier timed out")

                store.fault = fault
                return delivered(
                    Viewer(store).execute(
                        self.backend,
                        "viewer_read",
                        dict(lease_id=lease, session_ref=SESSION, query="", cursor=""),
                    )
                )

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(reading)
            self.assertTrue(entered.wait(2))
            try:
                self.refused("service_busy", self.close_lease, lease)
            finally:
                resume.set()
            self.assertEqual([], future.result()["data"]["items"])
        self.close_lease(lease)
        for _ in range(15):
            self.exchange()

        def attempting(_):
            try:
                self.exchange()
                return True
            except LocalError as exc:
                self.assertEqual("service_busy", exc.code)
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(attempting, range(8)))
        self.assertEqual(1, sum(results))
