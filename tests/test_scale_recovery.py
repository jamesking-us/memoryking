"""Backups and the job queue at store sizes far above the earlier limits (SQLite profile).

The large stores are built by inserting rows directly (events, observations, blobs, jobs) so the
tests cross the old failure thresholds without tens of thousands of signed capture requests.
"""

import hashlib
import statistics
import tempfile
import time
import unittest
from pathlib import Path

from test_journal import observation
from test_local_store import FakeClock

from memoryking import backups
from memoryking.adapters import blobs as blobfiles
from memoryking.adapters.local_files import atomic
from memoryking.adapters.local_store import SCHEMA_VERSION, LocalStore
from memoryking.contracts.canonical import MAX_BYTES, ContractError
from memoryking.domain.local import LocalError


def fabricate(store, count, *, jobs=False, shared_blob=None):
    """Insert `count` observations (distinct blobs unless `shared_blob`) after the current frontier."""
    tenant = "main"
    blobs = store.root / "blobs" / tenant
    start = store.db.execute("SELECT position FROM tenants WHERE tenant=?", (tenant,)).fetchone()[0]
    generation = store.db.execute(
        "SELECT generation FROM projections WHERE tenant=?", (tenant,)
    ).fetchone()[0]
    events, rows, queue = [], [], []
    for i in range(count):
        position = start + 1 + i
        if shared_blob is None:
            content = f"fabricated observation {position}".encode()
            digest = hashlib.sha256(content).hexdigest()
            path = blobs / digest
            path.write_bytes(content)
            path.chmod(0o600)
        else:
            digest = shared_blob
        events.append(
            (
                tenant,
                position,
                f"fab-evt-{position}",
                "observation_captured",
                f"fab-obs-{position}",
                store.incarnation,
                1,
                "owner",
            )
        )
        rows.append(
            (
                tenant,
                f"fab-obs-{position}",
                "demo",
                "owner",
                "fab",
                f"fab{position}",
                digest,
                digest,
                "null",
                position,
                store.incarnation,
                "internal",
                0,
            )
        )
        queue.append(
            (
                tenant,
                f"fab-job-{position}",
                position,
                f"fab-obs-{position}",
                store.incarnation,
                generation,
                "pending" if jobs else "complete",
                0,
                1,
                "owner",
            )
        )
    store.db.execute("BEGIN IMMEDIATE")
    store.db.executemany(
        "INSERT INTO events(tenant,position,event_id,kind,subject,origin,at_ms,actor) VALUES (?,?,?,?,?,?,?,?)",
        events,
    )
    store.db.executemany(
        "INSERT INTO observations(tenant,id,project,principal,source,source_event,payload_hash,"
        "blob,readers,position,origin,classification,observed_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    store.db.executemany(
        "INSERT INTO jobs(tenant,id,position,observation,incarnation,generation,state,purge_epoch,"
        "revision,principal) VALUES (?,?,?,?,?,?,?,?,?,?)",
        queue,
    )
    store.db.execute("UPDATE tenants SET position=? WHERE tenant=?", (start + count, tenant))
    store.db.execute("COMMIT")


class ScaleCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mk-scale-", dir="/tmp")
        self.base = Path(self.temp.name)
        self.root, self.control = self.base / "db", self.base / "ctl"
        self.credential = LocalStore.initialize(self.root, self.control).read_text()
        self.clock = FakeClock()
        self.store = LocalStore(self.root, self.control, clock=self.clock)
        self.namespace = self.store.namespace(self.credential, "demo")

    def tearDown(self):
        if self.store is not None:
            self.store.close()
        self.temp.cleanup()

    def capture(self, number, text="durable memory"):
        return self.store.capture(
            self.credential, self.namespace, f"k{number}", observation(number, text)
        )


class BackupScaleTests(ScaleCase):
    def test_backup_and_restore_round_trip_above_the_old_manifest_limit(self):
        self.capture(0, "the one real capture")
        fabricate(self.store, 20_000)
        backup = self.base / "backup"
        manifest = self.store.backup(backup)
        self.assertEqual(manifest["blob_index"]["count"], 20_001)
        self.assertNotIn("blobs", manifest)
        self.assertLess((backup / "manifest").stat().st_size, 4096)
        # The list the manifest used to embed would not have fitted the signing profile.
        self.assertGreater((backup / "blob-index").stat().st_size, MAX_BYTES)
        self.store.close()
        self.store = None
        LocalStore.restore(self.root, self.control, backup, clock=self.clock)
        self.store = LocalStore(self.root, self.control, clock=self.clock)
        self.assertEqual(
            20_001, self.store.db.execute("SELECT count(*) FROM observations").fetchone()[0]
        )
        # Restored blobs are sharded by hash prefix, and no flat duplicate remains.
        names = [name for name, _ in blobfiles.iterate(self.root / "blobs" / "main")]
        self.assertEqual(20_001, len(names))
        self.assertFalse(any(p.is_file() for p in (self.root / "blobs" / "main").iterdir()))


class BackupIntegrityTests(ScaleCase):
    def backup(self, *, blobs=3):
        for i in range(blobs):
            self.capture(i, f"distinct body {i}")
        backup = self.base / "backup"
        self.store.backup(backup)
        self.store.close()
        self.store = None
        return backup

    def restore(self, backup):
        LocalStore.restore(self.root, self.control, backup, clock=self.clock)
        self.store = LocalStore(self.root, self.control, clock=self.clock)

    def refused(self, backup):
        with self.assertRaises(LocalError) as caught:
            LocalStore.restore(self.root, self.control, backup, clock=self.clock)
        self.assertEqual("backup_invalid", caught.exception.code)

    def test_index_is_bound_by_hash_and_every_blob_by_the_index(self):
        backup = self.backup()
        index = (backup / "blob-index").read_bytes()
        lines = index.decode().splitlines()
        self.assertEqual(3, len(lines))
        for tampered in (
            "".join(line + "\n" for line in lines[:-1]).encode(),  # entry dropped
            index + (lines[0] + "\n").encode(),  # entry repeated
            index.replace(b"blobs/main/", b"blobs/other/", 1),  # entry redirected
            b"",
        ):
            atomic(backup / "blob-index", tampered)
            self.refused(backup)
        atomic(backup / "blob-index", index)
        blob = backup / lines[0]
        original = blob.read_bytes()
        atomic(blob, original + b"x")  # content no longer matches its index entry
        self.refused(backup)
        atomic(blob, original)
        (backup / "blob-index").unlink()
        with self.assertRaises(OSError):
            LocalStore.restore(self.root, self.control, backup, clock=self.clock)
        atomic(backup / "blob-index", index)
        self.restore(backup)
        self.assertEqual(
            3, self.store.db.execute("SELECT count(*) FROM observations").fetchone()[0]
        )

    def test_backup_written_by_the_previous_release_still_restores(self):
        backup = self.backup()
        lines = (backup / "blob-index").read_text().splitlines()
        manifest = self.control_of(backup)
        manifest.pop("blob_index")
        manifest.update(schema=8, blobs=lines)  # the earlier embedded-list layout
        (backup / "blob-index").unlink()
        atomic(backup / "manifest", self.signer().sign(manifest).encode())
        self.restore(backup)
        self.assertEqual(
            3, self.store.db.execute("SELECT count(*) FROM observations").fetchone()[0]
        )

    def signer(self):
        from memoryking.adapters.local_files import Control

        return Control(self.control)

    def control_of(self, backup):
        return self.signer().verify_document(backup / "manifest")


class FailedBackupTests(ScaleCase):
    def test_any_exception_leaves_no_directory_and_is_recorded(self):
        directory = self.base / "scheduled"
        policy = backups.Policy(directory, "1d", keep=2)

        class Exploding:
            def backup(self, target):
                target.mkdir()
                (target / "store.sqlite").write_bytes(b"partial")
                raise ContractError("document_too_large")  # a ValueError, not a LocalError

        self.assertIsNone(policy.run(Exploding(), self.clock.utc_ms()))
        self.assertEqual([], list(directory.iterdir()))
        self.assertEqual(self.clock.utc_ms(), policy.failed_ms)

    def test_the_daemon_hook_survives_such_a_failure(self):
        self.capture(1)
        self.store.backup_policy = backups.Policy(self.base / "scheduled", "1h", keep=2)

        def explode(target):
            target.mkdir()
            raise ValueError("unexpected")

        self.store.backup = explode
        self.store.housekeeping()
        self.assertEqual([], list((self.base / "scheduled").iterdir()))


class QueueScaleTests(ScaleCase):
    def test_advance_lookup_is_served_by_a_position_index(self):
        plan = " ".join(
            row[3]
            for row in self.store.db.execute(
                "EXPLAIN QUERY PLAN SELECT state FROM jobs WHERE tenant=? AND position=? "
                "AND incarnation=? AND generation=?",
                ("main", 1, "x", 1),
            )
        )
        self.assertIn("jobs_position", plan)

    def test_store_at_the_previous_schema_gains_the_index_on_open(self):
        self.store.db.execute("DROP INDEX jobs_position")
        self.store.db.execute("PRAGMA user_version=13")
        self.store.close()
        self.store = LocalStore(self.root, self.control, clock=self.clock)
        self.assertEqual(SCHEMA_VERSION, self.store.db.execute("PRAGMA user_version").fetchone()[0])
        names = {row[1] for row in self.store.db.execute("PRAGMA index_list(jobs)")}
        self.assertIn("jobs_position", names)

    def test_drain_time_per_job_stays_flat_across_30000_observations(self):
        self.capture(0)
        shared = self.store.db.execute("SELECT blob FROM observations").fetchone()[0]
        fabricate(self.store, 30_000, jobs=True, shared_blob=shared)
        passes, done = [], 0
        while True:
            started = time.perf_counter()
            completed = self.store.drain()
            elapsed = time.perf_counter() - started
            if not completed:
                break
            done += completed
            passes.append(elapsed / completed)
        self.assertEqual(30_001, done)
        self.assertGreater(len(passes), 100)
        early, late = statistics.median(passes[:20]), statistics.median(passes[-20:])
        self.assertLess(
            late, max(early * 4, 0.002), f"early {early:.5f}s, late {late:.5f}s per job"
        )
        self.assertLess(max(passes) * 256, 2.0, "every pass fits well inside the 5 s deadline")
        frontier = self.store.db.execute("SELECT frontier FROM projections").fetchone()[0]
        self.assertEqual(30_001, frontier)

    def test_capture_straight_after_reindex_of_2000_observations_is_accepted(self):
        self.capture(0)
        fabricate(self.store, 2_000)
        self.store.reindex(self.credential)
        # The call itself resets nothing at this size; the batches of the rebuild reset the
        # jobs as they pass, and the reset backlog never counts against the queue limit.
        while not self.store.rebuild_lexical(wall_ms=1500):
            pass
        pending = self.store.db.execute(
            "SELECT count(*) FROM jobs WHERE state='pending'"
        ).fetchone()[0]
        self.assertGreaterEqual(pending, 2_000)
        self.capture(1, "captured right after the reindex")
        while self.store.drain():
            pass
        self.assertEqual(
            0,
            self.store.db.execute("SELECT count(*) FROM jobs WHERE state='pending'").fetchone()[0],
        )

    def test_fresh_captures_are_still_refused_at_the_queue_limit(self):
        self.capture(0)
        shared = self.store.db.execute("SELECT blob FROM observations").fetchone()[0]
        fabricate(self.store, 1_000, jobs=True, shared_blob=shared)
        with self.assertRaises(LocalError) as caught:
            self.capture(1)
        self.assertEqual("queue_full", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
