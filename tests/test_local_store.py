"""Product tests against actual SQLite, durable files, clocks and real processes."""

import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from memoryking.adapters.local_files import atomic
from memoryking.adapters.local_ipc import Server, request
from memoryking.adapters.local_store import RETRY_MS, LocalStore
from memoryking.domain.local import LocalError, Released


class FakeClock:
    def __init__(self):
        self.utc = 2_000_000_000_000
        self.mono = 1000

    def utc_ms(self):
        return self.utc

    def monotonic_ms(self):
        return self.mono

    def advance(self, ms):
        self.utc += ms
        self.mono += ms


def observation(number=1, text="durable memory", **extra):
    return {
        "version": 1,
        "project": "demo",
        "source": "test",
        "source_event": f"e{number}",
        "text": text,
        "readers": None,
        "classification": "internal",
        "observed_at_ms": 0,
        **extra,
    }


def delivered(packet: Released):
    received = []
    packet.emit(received.append)
    return json.loads(received[0])


class LocalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mk-", dir="/tmp")
        self.base = Path(self.temp.name)
        self.root, self.control = self.base / "db", self.base / "ctl"
        self.credential_path = LocalStore.initialize(self.root, self.control)
        self.credential = self.credential_path.read_text()
        self.clock = FakeClock()
        self.store = LocalStore(self.root, self.control, clock=self.clock)
        self.namespace = self.store.namespace(self.credential, "demo")

    def tearDown(self):
        if self.store:
            self.store.close()
        self.temp.cleanup()

    def capture(self, number=1, **kwargs):
        return self.store.capture(
            self.credential, self.namespace, f"key{number}", observation(number, **kwargs)
        )

    def error(self, code, fn, *args, **kwargs):
        with self.assertRaises(LocalError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)

    def restart(self):
        self.store.close()
        self.store = None
        self.store = LocalStore(self.root, self.control, clock=self.clock)

    def test_capture_atomic_fts_occurrence_audit_and_replay(self):
        first = self.capture()
        self.assertEqual(first, self.capture())
        other_ns = self.store.namespace(self.credential, "demo")
        self.assertEqual(
            first, self.store.capture(self.credential, other_ns, "otherkey", observation())
        )
        self.assertEqual(
            1, self.store.db.execute("SELECT count(*) FROM observations").fetchone()[0]
        )
        self.assertEqual(1, self.store.db.execute("SELECT count(*) FROM events").fetchone()[0])
        self.assertEqual(1, self.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0])
        self.assertEqual(
            "durable memory",
            delivered(self.store.read(self.credential, "demo", query="durable"))["items"][0][
                "text"
            ],
        )
        self.restart()
        self.assertEqual(first, self.capture())

    def test_concurrent_equal_and_unequal_keys(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.capture(), range(16)))
        self.assertTrue(all(result == results[0] for result in results))
        self.error("idempotency_conflict", self.capture, text="different")
        self.error(
            "source_conflict",
            self.store.capture,
            self.credential,
            self.namespace,
            "newkey",
            observation(text="different"),
        )
        self.assertEqual(1, self.store.db.execute("SELECT position FROM tenants").fetchone()[0])

    def test_result_expiry_and_purge_retain_tombstones(self):
        result = self.capture()
        self.clock.advance(RETRY_MS)
        self.assertEqual(1, self.store.expire_results())
        self.error("idempotency_result_expired", self.capture)
        self.store.purge(self.credential, result["observation"])
        self.error(
            "source_retired",
            self.store.capture,
            self.credential,
            self.namespace,
            "newkey",
            observation(),
        )
        self.assertEqual(0, self.store.db.execute("SELECT count(*) FROM lexical").fetchone()[0])
        self.assertEqual([], list((self.root / "blobs" / "main").iterdir()))

    def test_shared_blob_purge_preserves_other_occurrence(self):
        first = self.capture()
        second = self.capture(2)
        self.store.purge(self.credential, first["observation"])
        self.assertEqual(1, len(list((self.root / "blobs" / "main").iterdir())))
        self.assertEqual(
            second["observation"],
            delivered(self.store.read(self.credential, "demo"))["items"][0]["id"],
        )

    def test_authorization_scope_audience_and_current_retry_check(self):
        writer = self.store.grant(self.credential, "writer", "demo", ["capture", "read"])
        ns = self.store.namespace(writer, "demo")
        item = self.store.capture(writer, ns, "k", observation(readers=["writer"]))
        reader = self.store.grant(self.credential, "reader", "demo", ["read", "export"])
        self.assertEqual([], delivered(self.store.read(reader, "demo"))["items"])
        self.error("unavailable", self.store.read, reader, "demo", observation=item["handle"])
        self.error("denied", self.store.read, reader, "other")
        self.error("denied", self.store.namespace, reader, "demo")
        self.error("denied", self.store.status, reader, "demo")
        self.store.revoke(self.credential, "writer", "demo")
        self.error("denied", self.store.capture, writer, ns, "k", observation(readers=["writer"]))
        self.error("denied", self.store.read, writer, "demo")

    def test_receipt_authorization_instant_and_release_deadline(self):
        self.capture()
        start = self.clock.utc
        token = self.store.grant(
            self.credential, "reader", "demo", ["read"], starts=start, expires=start + 1000
        )
        self.store.fault = lambda point: (
            self.clock.advance(2000) if point == "after_validation" else None
        )
        response = self.store.read(token, "demo")
        actual = delivered(response)
        self.assertEqual(start, actual["evaluated_ms"])
        persisted = self.store.db.execute("SELECT evaluated,deadline FROM receipts").fetchone()
        self.assertEqual((start, start + 5000), tuple(persisted))
        self.store.fault = lambda _: None
        self.error("denied", self.store.read, token, "demo")
        response = self.store.read(self.credential, "demo")
        self.clock.advance(5000)
        sent = []
        self.error("release_expired", response.emit, sent.append)
        self.assertEqual([], sent)

    def test_validation_stall_rollback_and_temporal_inputs(self):
        self.capture()
        for requirement in ("constraint", "verification"):
            with self.subTest(requirement=requirement):
                start = self.clock.utc
                self.store.fault = lambda point: (
                    self.clock.advance(1000) if point == "after_validation" else None
                )
                response = self.store.read(
                    self.credential, "demo", temporal_requirements=[(start, start + 1000)]
                )
                self.assertTrue(delivered(response)["items"])
                self.store.fault = lambda _: None
                self.error(
                    "temporal_requirement_failed",
                    self.store.read,
                    self.credential,
                    "demo",
                    temporal_requirements=[(start, start + 1000)],
                )
                self.error(
                    "temporal_requirement_failed",
                    self.store.read,
                    self.credential,
                    "demo",
                    temporal_requirements=[(self.clock.utc + 1, None)],
                )
        before = self.store.db.execute("SELECT count(*) FROM receipts").fetchone()[0]
        self.store.fault = lambda point: (
            self.clock.advance(5000) if point == "after_validation" else None
        )
        self.error("release_expired", self.store.read, self.credential, "demo")
        self.assertEqual(
            before, self.store.db.execute("SELECT count(*) FROM receipts").fetchone()[0]
        )

    def test_backward_clock_fails_current_and_persisted_decisions(self):
        self.capture()
        packet = self.store.read(self.credential, "demo")
        self.clock.utc -= 1
        self.error("clock_untrusted", delivered, packet)
        self.error("clock_untrusted", self.capture, 2)
        self.restart()
        self.error("clock_untrusted", self.capture, 2)

    def test_policy_commit_orders_before_next_delivery_gate(self):
        self.capture()
        token = self.store.grant(self.credential, "reader", "demo", ["read"])
        packet = self.store.read(token, "demo")
        self.store.revoke(self.credential, "reader", "demo")
        self.assertTrue(delivered(packet)["items"])  # previously committed bounded decision
        self.error("denied", self.store.read, token, "demo")

    def test_projection_gap_cancel_reindex_and_stale_fence(self):
        first, second = self.capture(), self.capture(2)
        one, two = [self.store.claim(self.credential, "demo") for _ in range(2)]
        self.store.finish(self.credential, "demo", two["lease"], "complete")
        self.assertFalse(
            self.store.status(self.credential, "demo", second["watermark"])["satisfied"]
        )
        self.store.finish(self.credential, "demo", one["lease"], "cancel")
        self.assertEqual(0, self.store.status(self.credential, "demo")["projection"]["frontier"])
        self.store.reindex(self.credential)
        self.error(
            "stale_lease", self.store.finish, self.credential, "demo", one["lease"], "complete"
        )
        for _ in range(2):
            lease = self.store.claim(self.credential, "demo")
            self.store.finish(self.credential, "demo", lease["lease"], "complete")
        status = self.store.status(self.credential, "demo", first["watermark"])
        self.assertTrue(status["satisfied"])
        self.assertEqual(2, status["projection"]["frontier"])
        self.assertEqual(2, status["projection"]["generation"])
        self.assertEqual(
            2, self.store.db.execute("SELECT count(*) FROM projection_items").fetchone()[0]
        )
        self.assertEqual(
            len(b"durable memory"),
            self.store.db.execute("SELECT byte_count FROM projection_items LIMIT 1").fetchone()[0],
        )

    def test_job_lease_expiry_retry_dead_letter_and_purge(self):
        first = self.capture()
        old = self.store.claim(self.credential, "demo")
        self.clock.advance(30000)
        new = self.store.claim(self.credential, "demo")
        self.error(
            "stale_lease", self.store.finish, self.credential, "demo", old["lease"], "complete"
        )
        self.store.finish(self.credential, "demo", new["lease"], "retry")
        self.assertIsNone(self.store.claim(self.credential, "demo"))
        for _ in range(3):
            self.clock.advance(61000)
            lease = self.store.claim(self.credential, "demo")
            self.store.finish(self.credential, "demo", lease["lease"], "retry")
        self.assertEqual(
            "dead",
            self.store.db.execute(
                "SELECT state FROM jobs WHERE observation IS NOT NULL"
            ).fetchone()[0],
        )
        self.assertEqual(0, self.store.status(self.credential, "demo")["projection"]["frontier"])
        self.store.purge(self.credential, first["observation"])
        self.assertEqual(2, self.store.status(self.credential, "demo")["projection"]["frontier"])

    def test_owner_permissions_and_tampered_handles(self):
        self.error("owner_busy_or_unsafe", LocalStore, self.root, self.control)
        self.error("invalid_token", self.store.namespace, self.credential + "bad", "demo")
        self.assertEqual(0o700, self.root.stat().st_mode & 0o777)
        self.assertEqual(0o600, (self.root / "store.sqlite").stat().st_mode & 0o777)
        self.credential_path.chmod(0o644)
        from memoryking.adapters.local_files import private_file

        self.error("unsafe_permissions", private_file, self.credential_path)

    def test_restore_old_history_all_handles_and_current_journal(self):
        first = self.capture()
        delegated = self.store.grant(self.credential, "reader", "demo", ["read"])
        cursor = delivered(self.store.read(self.credential, "demo"))["cursor"]
        lease = self.store.claim(self.credential, "demo")
        backup = self.base / "backup"
        self.store.backup(backup)
        second = self.capture(2)
        self.store.purge(self.credential, first["observation"])
        self.store.revoke(self.credential, "reader", "demo")
        old_incarnation = self.store.incarnation
        self.store.close()
        self.store = None
        LocalStore.restore(self.root, self.control, backup)
        self.credential = self.credential_path.read_text()
        self.store = LocalStore(self.root, self.control, clock=self.clock)
        self.assertNotEqual(old_incarnation, self.store.incarnation)
        self.assertEqual([], delivered(self.store.read(self.credential, "demo"))["items"])
        self.error("history_changed", self.store.read, delegated, "demo")
        self.error("history_changed", self.store.read, self.credential, "demo", cursor=cursor)
        self.error(
            "history_changed",
            self.store.capture,
            self.credential,
            self.namespace,
            "key1",
            observation(),
        )
        self.error(
            "history_changed",
            self.store.capture,
            self.credential,
            self.namespace,
            "key2",
            observation(2),
        )
        self.error(
            "history_changed",
            self.store.finish,
            self.credential,
            "demo",
            lease["lease"],
            "complete",
        )
        self.namespace = self.store.namespace(self.credential, "demo")
        for number in range(3, 10):
            self.capture(number)
        self.error(
            "history_changed", self.store.status, self.credential, "demo", second["watermark"]
        )
        self.error(
            "history_changed", self.store.status, self.credential, "demo", first["watermark"]
        )
        self.error(
            "source_retired",
            self.store.capture,
            self.credential,
            self.namespace,
            "freshkey",
            observation(),
        )
        current_reader = self.store._token("credential", tenant="main", principal="reader")
        self.error("denied", self.store.read, current_reader, "demo")

    def test_purge_of_lost_occurrence_survives_restore(self):
        backup = self.base / "backup"
        self.store.backup(backup)
        later = self.capture()
        self.store.purge(self.credential, later["observation"])
        self.store.close()
        self.store = None
        LocalStore.restore(self.root, self.control, backup)
        self.store = LocalStore(self.root, self.control, clock=self.clock)
        self.credential = self.credential_path.read_text()
        self.namespace = self.store.namespace(self.credential, "demo")
        self.error("source_retired", self.capture)

    def test_large_archive_manifest_restores_without_widening_credential_limits(self):
        observations = [self.capture(i, text=f"Distinct archive body {i}") for i in range(90)]
        backup = self.base / "backup"
        self.store.backup(backup)
        manifest_path = backup / "manifest"
        manifest = manifest_path.read_bytes()
        self.assertGreater(len(manifest), 8192)
        self.error("invalid_token", self.store.control.verify, manifest.decode())
        oversized = self.base / "oversized-manifest"
        atomic(oversized, b"x" * (16 * 1024 * 1024 + 1))
        self.error("invalid_token", self.store.control.verify_document, oversized)
        first = observations[0]["observation"]
        self.store.purge(self.credential, first)
        old_history, old_credential = self.store.incarnation, self.credential
        self.store.close()
        self.store = None
        atomic(manifest_path, manifest[:-1] + (b"0" if manifest[-1:] != b"0" else b"1"))
        self.error("invalid_token", LocalStore.restore, self.root, self.control, backup)
        atomic(manifest_path, manifest)
        LocalStore.restore(self.root, self.control, backup, clock=self.clock)
        self.store = LocalStore(self.root, self.control, clock=self.clock)
        self.credential = self.credential_path.read_text()
        self.namespace = self.store.namespace(self.credential, "demo")
        self.assertNotEqual(old_history, self.store.incarnation)
        self.assertEqual(
            90, self.store.db.execute("SELECT count(*) FROM observations").fetchone()[0]
        )
        self.assertEqual(
            1,
            self.store.db.execute(
                "SELECT deleted FROM observations WHERE id=?", (first,)
            ).fetchone()[0],
        )
        self.error("history_changed", self.store.read, old_credential, "demo")
        self.error("source_retired", self.capture, 0, text="Distinct archive body 0")

    def test_silent_database_rollback_and_missing_anchor_fail_closed(self):
        backup = self.base / "backup"
        self.store.backup(backup)
        self.capture()
        self.store.close()
        self.store = None
        atomic(self.root / "store.sqlite", (backup / "store.sqlite").read_bytes())
        self.error("recovery_required", LocalStore, self.root, self.control)
        (self.control / "anchor").unlink()
        self.error("recovery_required", LocalStore, self.root, self.control)
        self.error("recovery_required", LocalStore.restore, self.root, self.control, backup)

    def test_journal_rollback_and_backup_corruption_fail_closed(self):
        backup = self.base / "backup"
        self.store.backup(backup)
        self.store.grant(self.credential, "a", "demo", ["read"])
        self.store.close()
        self.store = None
        original = (self.control / "journal").read_bytes()
        atomic(self.control / "journal", b"")
        self.error("journal_rollback", LocalStore, self.root, self.control)
        atomic(self.control / "journal", original)
        atomic(backup / "store.sqlite", b"damaged")
        self.error("backup_invalid", LocalStore.restore, self.root, self.control, backup)

    def test_interrupted_activation_stays_unavailable_then_explicit_retry(self):
        backup = self.base / "backup"
        self.capture()
        self.store.backup(backup)
        self.store.close()
        self.store = None

        def interrupt(point):
            if point == "restore_before_activation":
                raise RuntimeError("injected")

        with self.assertRaises(RuntimeError):
            LocalStore.restore(self.root, self.control, backup, fault=interrupt)
        self.error("recovery_required", LocalStore, self.root, self.control)
        LocalStore.restore(self.root, self.control, backup)
        self.store = LocalStore(self.root, self.control, clock=self.clock)
        self.assertEqual(
            1, self.store.db.execute("SELECT count(*) FROM observations").fetchone()[0]
        )

    def test_policy_failure_does_not_leave_old_authority_serving(self):
        token = self.store.grant(self.credential, "reader", "demo", ["read"])

        def interrupt(point):
            if point == "after_journal":
                raise RuntimeError("injected")

        self.store.fault = interrupt
        with self.assertRaises(RuntimeError):
            self.store.revoke(self.credential, "reader", "demo")
        self.error("recovery_required", self.store.read, token, "demo")
        self.restart()
        self.error("denied", self.store.read, token, "demo")

    def test_real_process_crashes_at_blob_commit_and_anchor_boundaries(self):
        script = """
import os, sys
from pathlib import Path
from memoryking.adapters.local_store import LocalStore
root, ctl, point, namespace = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
def fail(stage):
    if stage == point: os._exit(73)
with LocalStore(root, ctl, fault=fail) as store:
    store.capture((ctl/'owner.credential').read_text(), namespace, 'crashkey', {'version':1,'project':'demo','source':'crash','source_event':point,'text':'crash evidence','readers':None,'classification':'internal','observed_at_ms':0})
"""
        for point in (
            "after_blob",
            "before_commit",
            "after_commit",
            "before_anchor",
            "after_anchor",
        ):
            with self.subTest(point=point):
                root, ctl = self.base / (point + "db"), self.base / (point + "ctl")
                credential = LocalStore.initialize(root, ctl).read_text()
                with LocalStore(root, ctl) as store:
                    namespace = store.namespace(credential, "demo")
                result = subprocess.run(
                    [sys.executable, "-c", script, str(root), str(ctl), point, namespace],
                    capture_output=True,
                    env={
                        k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}
                    },
                )
                self.assertEqual(73, result.returncode, result.stderr.decode())
                with LocalStore(root, ctl) as store:
                    body = observation(source="crash", source_event=point, text="crash evidence")
                    first = store.capture(credential, namespace, "crashkey", body)
                    self.assertEqual(first, store.capture(credential, namespace, "crashkey", body))
                    self.assertEqual(
                        1, store.db.execute("SELECT count(*) FROM observations").fetchone()[0]
                    )
                    self.assertEqual(1, len(list((root / "blobs" / "main").iterdir())))

    def test_real_socket_protocol_and_export_reauthorization(self):
        self.capture()
        with Server(self.store) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                self.assertEqual(0o600, server.path.stat().st_mode & 0o777)
                result = request(server.path, self.credential, "read", {"project": "demo"})
                self.assertEqual("durable memory", result["items"][0]["text"])
                self.error(
                    "invalid_request",
                    request,
                    server.path,
                    self.credential,
                    "read",
                    {"project": "demo", "claimed_role": "admin"},
                )
                self.error(
                    "invalid_request",
                    request,
                    server.path,
                    self.credential,
                    "capture",
                    {"namespace": self.namespace, "key": "bad", "request": observation(version=2)},
                )
                reader = self.store.grant(self.credential, "exporter", "demo", ["export", "read"])
                page = request(server.path, reader, "export", {"project": "demo"})
                self.store.revoke(self.credential, "exporter", "demo")
                self.error(
                    "denied",
                    request,
                    server.path,
                    reader,
                    "export",
                    {"project": "demo", "cursor": page["cursor"]},
                )
            finally:
                server.shutdown()
                thread.join(2)

    def test_blob_tampering_cannot_be_released(self):
        self.capture()
        path = self.root / "blobs" / "main" / hashlib.sha256(b"durable memory").hexdigest()
        atomic(path, b"wrong")
        self.error("blob_corrupt", self.store.read, self.credential, "demo")

    def test_lost_anchor_requires_independent_current_journal_checkpoint(self):
        self.capture()
        backup = self.base / "backup"
        self.store.backup(backup)
        self.store.grant(self.credential, "reader", "demo", ["read"])
        checkpoint = self.store.anchor["journal_head"]
        old_incarnation = self.store.incarnation
        self.store.close()
        self.store = None
        (self.control / "anchor").unlink()
        self.error(
            "recovery_checkpoint_invalid",
            LocalStore.restore,
            self.root,
            self.control,
            backup,
            lost_anchor_journal_head="0" * 64,
        )
        LocalStore.restore(self.root, self.control, backup, lost_anchor_journal_head=checkpoint)
        self.store = LocalStore(self.root, self.control, clock=self.clock)
        self.assertTrue(self.store.anchor["history_loss"])
        self.assertNotEqual(old_incarnation, self.store.incarnation)
        self.error("history_changed", self.store.namespace, self.credential, "demo")

    def test_rollback_of_job_only_commit_is_detected(self):
        self.capture()
        backup = self.base / "backup"
        self.store.backup(backup)
        self.store.claim(self.credential, "demo")
        self.store.close()
        self.store = None
        atomic(self.root / "store.sqlite", (backup / "store.sqlite").read_bytes())
        self.error("recovery_required", LocalStore, self.root, self.control)

    def test_old_observation_handle_and_cursor_query_binding(self):
        captured = self.capture()
        cursor = delivered(self.store.read(self.credential, "demo", query="durable"))["cursor"]
        self.error(
            "denied", self.store.read, self.credential, "demo", cursor=cursor, query="different"
        )
        backup = self.base / "backup"
        self.store.backup(backup)
        self.store.close()
        self.store = None
        LocalStore.restore(self.root, self.control, backup)
        self.store = LocalStore(self.root, self.control, clock=self.clock)
        self.credential = self.credential_path.read_text()
        self.error(
            "history_changed",
            self.store.read,
            self.credential,
            "demo",
            observation=captured["handle"],
        )

    def test_cross_store_tokens_and_tenant_qualified_foreign_keys(self):
        import sqlite3

        other_root, other_control = self.base / "other", self.base / "other-control"
        token = LocalStore.initialize(other_root, other_control, "other").read_text()
        self.error("invalid_token", self.store.namespace, token, "demo")
        item = self.capture()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute(
                "INSERT INTO jobs(tenant,id,position,observation,incarnation,generation,state,purge_epoch,revision,principal) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "foreign",
                    "j",
                    1,
                    item["observation"],
                    self.store.incarnation,
                    1,
                    "pending",
                    0,
                    1,
                    "owner",
                ),
            )

    def test_queue_and_worker_bounds_and_current_source_permission(self):
        from unittest.mock import patch

        with patch("memoryking.application.store_core.QUEUE_LIMIT", 1):
            self.capture()
            self.error("queue_full", self.capture, 2)
        for number in range(2, 6):
            self.capture(number)
        leases = [self.store.claim(self.credential, "demo") for _ in range(4)]
        self.error("concurrency_limit", self.store.claim, self.credential, "demo")
        self.store.purge(
            self.credential,
            self.store.db.execute(
                "SELECT id FROM observations ORDER BY position LIMIT 1"
            ).fetchone()[0],
        )
        self.error(
            "stale_lease",
            self.store.finish,
            self.credential,
            "demo",
            leases[0]["lease"],
            "complete",
        )
        worker = self.store.grant(self.credential, "worker", "demo", ["jobs"])
        next_lease = self.store.claim(worker, "demo")
        self.store.revoke(self.credential, "worker", "demo")
        self.error("denied", self.store.finish, worker, "demo", next_lease["lease"], "complete")

    def test_receipts_expire_after_thirty_days_through_hourly_housekeeping(self):
        self.capture()
        delivered(self.store.read(self.credential, "demo"))
        count = lambda: self.store.db.execute("SELECT count(*) FROM receipts").fetchone()[0]  # noqa: E731
        self.assertEqual(count(), 1)
        self.assertTrue(self.store.housekeeping())
        self.assertFalse(self.store.housekeeping(), "housekeeping runs at most once per hour")
        self.assertEqual(count(), 1)
        self.clock.advance(RETRY_MS + 1)
        self.assertTrue(self.store.housekeeping())
        self.assertEqual(count(), 0)

    def test_daemon_drains_audit_jobs_so_default_captures_never_hit_queue_full(self):
        # Default configuration, no patched limits, no separate worker: the serving
        # daemon completes audit_probe jobs between requests.
        with Server(self.store) as server:
            thread = threading.Thread(
                target=lambda: server.serve_forever(poll_interval=0.005), daemon=True
            )
            thread.start()
            try:
                for number in range(1, 1002):
                    self.capture(number)
            finally:
                server.shutdown()
                thread.join(timeout=5)
        self.store.drain()
        states = dict(self.store.db.execute("SELECT state,count(*) FROM jobs GROUP BY state"))
        self.assertEqual(states, {"complete": 1001})
        status = self.store.status(self.credential, "demo")
        self.assertEqual(status["projection"]["frontier"], status["position"])
        self.assertEqual(status["capacity"]["jobs"], {"used": 0, "limit": 1000, "warning": False})
        self.assertEqual(
            set(status["capacity"]),
            {
                "jobs",
                "rendered_artifacts",
                "rendered_bytes",
                "operation_intents",
                "processing_usage",
            },
        )

    def test_anchor_write_failure_returns_no_ack_and_retry_recovers(self):
        def fail(point):
            if point == "before_anchor":
                raise OSError("synthetic write failure")

        self.store.fault = fail
        with self.assertRaises(OSError):
            self.capture()
        self.error("recovery_required", self.capture)
        self.restart()
        self.capture()
        self.assertEqual(
            1, self.store.db.execute("SELECT count(*) FROM observations").fetchone()[0]
        )

    def test_post_commit_stall_does_not_emit_and_counter_never_wraps(self):
        self.capture()
        self.store.fault = lambda point: (
            self.clock.advance(5000) if point == "after_commit" else None
        )
        self.error("release_expired", self.store.read, self.credential, "demo")
        self.assertEqual(1, self.store.db.execute("SELECT count(*) FROM receipts").fetchone()[0])
        self.store.fault = lambda _: None
        from memoryking.adapters.local_store import MAX_COUNTER

        self.store.db.execute("UPDATE tenants SET position=?", (MAX_COUNTER,))
        self.error("counter_exhausted", self.capture, 2)
        self.assertEqual(
            MAX_COUNTER, self.store.db.execute("SELECT position FROM tenants").fetchone()[0]
        )


if __name__ == "__main__":
    unittest.main()
