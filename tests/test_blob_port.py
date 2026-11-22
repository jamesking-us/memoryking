"""The blob-store port: one contract run against every implementation, and the store itself run
on a second implementation that keeps blobs in memory (a stand-in for any backend that is not a
local directory). A new blob store passes `BlobStoreContract` and then the store slice below."""

import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_handoff
import test_knowledge
import test_local_store
from blob_helpers import MemoryBlobStore

from memoryking.adapters import local_store
from memoryking.adapters.blobs import FileBlobStore
from memoryking.adapters.local_store import LocalStore
from memoryking.domain.local import LocalError


class BlobStoreContract:
    """Everything the store core relies on. Subclasses provide `make`."""

    def make(self):
        raise NotImplementedError

    def setUp(self):
        self.blobs = self.make()

    def test_put_returns_the_digest_and_is_idempotent(self):
        digest = self.blobs.put("t1", b"hello")
        self.assertEqual(hashlib.sha256(b"hello").hexdigest(), digest)
        self.assertEqual(digest, self.blobs.put("t1", b"hello"))
        self.assertEqual([digest], self.blobs.digests("t1"))
        self.assertEqual(b"hello", self.blobs.get("t1", digest))

    def test_head_reads_a_bounded_prefix(self):
        digest = self.blobs.put("t1", b"abcdefgh")
        self.assertEqual(b"abc", self.blobs.head("t1", digest, 3))
        self.assertEqual(b"abcdefgh", self.blobs.head("t1", digest, 100))

    def test_a_blob_that_is_not_held_is_not_found(self):
        with self.assertRaises(OSError):
            self.blobs.get("t1", hashlib.sha256(b"absent").hexdigest())
        with self.assertRaises(OSError):
            self.blobs.copy_to("t1", hashlib.sha256(b"absent").hexdigest(), self.scratch / "x")

    def test_tenants_hold_separate_blobs(self):
        digest = self.blobs.put("t1", b"same bytes")
        self.assertEqual(digest, self.blobs.put("t2", b"same bytes"))
        self.assertEqual(["t1", "t2"], sorted(self.blobs.tenants()))
        self.assertEqual(1, self.blobs.delete("t1", digest))
        self.assertEqual([], self.blobs.digests("t1"))
        self.assertEqual(b"same bytes", self.blobs.get("t2", digest))

    def test_delete_reports_whether_it_removed_anything(self):
        digest = self.blobs.put("t1", b"gone soon")
        self.assertEqual(1, self.blobs.delete("t1", digest))
        self.assertEqual(0, self.blobs.delete("t1", digest))
        self.blobs.sync()
        self.assertEqual([], self.blobs.digests("t1"))

    def test_put_stream_verifies_the_name(self):
        digest = hashlib.sha256(b"restored").hexdigest()
        self.blobs.put_stream("t1", digest, io.BytesIO(b"restored"))
        self.blobs.sync()
        self.assertEqual(b"restored", self.blobs.get("t1", digest))
        wrong = hashlib.sha256(b"other").hexdigest()
        with self.assertRaises(LocalError) as caught:
            self.blobs.put_stream("t1", wrong, io.BytesIO(b"restored"))
        self.assertEqual("blob_corrupt", caught.exception.code)
        self.assertNotIn(wrong, self.blobs.digests("t1"))

    def test_copy_to_writes_a_verified_copy(self):
        digest = self.blobs.put("t1", b"for a backup")
        target = self.scratch / "copy"
        self.assertEqual(len(b"for a backup"), self.blobs.copy_to("t1", digest, target))
        self.assertEqual(b"for a backup", target.read_bytes())

    def test_maintain_finishes(self):
        self.blobs.put("t1", b"x")
        self.assertTrue(self.blobs.maintain(budget_s=5))


class FileBlobStoreTests(BlobStoreContract, unittest.TestCase):
    def make(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mk-blobs-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.scratch = Path(self.temp.name)
        root = self.scratch / "root"
        root.mkdir(mode=0o700)
        (root / "blobs").mkdir(mode=0o700)
        return FileBlobStore(root)

    def test_a_blob_whose_bytes_changed_is_refused(self):
        digest = self.blobs.put("t1", b"original")
        path = next((self.scratch / "root" / "blobs" / "t1").rglob(digest))
        path.write_bytes(b"tampered")
        with self.assertRaises(LocalError) as caught:
            self.blobs.get("t1", digest)
        self.assertEqual("blob_corrupt", caught.exception.code)
        with self.assertRaises(LocalError):
            self.blobs.put("t1", b"original")


class MemoryBlobStoreTests(BlobStoreContract, unittest.TestCase):
    def make(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mk-blobs-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.scratch = Path(self.temp.name)
        return MemoryBlobStore()


class OnMemoryBlobs:
    """Mixin: every store a test opens or restores keeps its blobs in one in-memory store,
    which outlives the store objects the way a directory would."""

    def setUp(self):
        self.memory = MemoryBlobStore()
        patch = mock.patch.object(local_store, "FileBlobStore", lambda root: self.memory)
        patch.start()
        self.addCleanup(patch.stop)
        super().setUp()

    def test_no_blob_file_is_written(self):
        self.assertEqual([], [p for p in (self.root / "blobs").rglob("*") if p.is_file()])


# The slice of the store's own cases that exercise blobs: capture and recall, replay, purge,
# a purge that must not remove a blob another observation still names, the projection worker
# that reads blobs, and backup, restore and the purge that survives a restore.
LOCAL_SLICE = (
    "test_capture_atomic_fts_occurrence_audit_and_replay",
    "test_concurrent_equal_and_unequal_keys",
    "test_result_expiry_and_purge_retain_tombstones",
    "test_job_lease_expiry_retry_dead_letter_and_purge",
    "test_projection_gap_cancel_reindex_and_stale_fence",
    "test_restore_old_history_all_handles_and_current_journal",
    "test_purge_of_lost_occurrence_survives_restore",
    "test_large_archive_manifest_restores_without_widening_credential_limits",
    "test_daemon_drains_audit_jobs_so_default_captures_never_hit_queue_full",
)


def only(base, names):
    """A subclass of the shared case class that runs just the named cases."""
    attributes = {
        name: None for name in dir(base) if name.startswith("test_") and name not in names
    }
    return attributes


class LocalOnMemoryBlobs(OnMemoryBlobs, test_local_store.LocalTests):
    locals().update(only(test_local_store.LocalTests, LOCAL_SLICE))

    def test_a_blob_another_observation_names_survives_a_purge(self):
        first = self.capture()
        second = self.capture(2)
        self.assertNotEqual(first["observation"], second["observation"])
        self.assertEqual(1, len(self.memory.digests("main")))
        self.store.purge(self.credential, first["observation"])
        self.assertEqual(1, len(self.memory.digests("main")))
        items = test_local_store.delivered(self.store.read(self.credential, "demo"))["items"]
        self.assertEqual(second["observation"], items[0]["id"])
        self.store.purge(self.credential, second["observation"])
        self.assertEqual([], self.memory.digests("main"))

    def test_gc_sweeps_blobs_no_observation_names(self):
        self.capture()
        orphan = self.memory.put("main", b"an orphan nothing names")
        self.assertEqual(1, self.store.gc())
        self.assertNotIn(orphan, self.memory.digests("main"))
        self.assertEqual(1, len(self.memory.digests("main")))

    def test_a_blob_changed_in_the_store_cannot_be_released(self):
        result = self.capture()
        digest = self.store.db.execute("SELECT blob FROM observations").fetchone()[0]
        self.memory.blobs[("main", digest)] = b"tampered"
        with self.assertRaises(LocalError) as caught:
            self.store.read(self.credential, "demo")
        self.assertEqual("blob_corrupt", caught.exception.code)
        self.assertTrue(result)

    def test_backup_and_restore_carry_the_blobs(self):
        self.capture()
        self.capture(2, text="a second body")
        before = sorted(self.memory.digests("main"))
        backup = self.base / "backup"
        self.store.backup(backup)
        manifest = self.store.control.verify_document(backup / "manifest")
        self.assertEqual(2, manifest["blobs_copied"])
        self.store.close()
        self.store = None
        self.memory.blobs.clear()
        credential = LocalStore.restore(self.root, self.control, backup).read_text()
        self.assertEqual(before, sorted(self.memory.digests("main")))
        with LocalStore(self.root, self.control) as restored:
            items = test_local_store.delivered(restored.read(credential, "demo"))["items"]
        self.assertEqual({"durable memory", "a second body"}, {i["text"] for i in items})


class HandoffOnMemoryBlobs(OnMemoryBlobs, test_handoff.HandoffTests):
    locals().update(
        only(
            test_handoff.HandoffTests,
            ("test_purge_erases_handoff_copies_and_restore_retires_old_history",),
        )
    )


class KnowledgeOnMemoryBlobs(OnMemoryBlobs, test_knowledge.KnowledgeTests):
    locals().update(
        only(
            test_knowledge.KnowledgeTests,
            ("test_evidence_revocation_purge_and_restore",),
        )
    )


if __name__ == "__main__":
    unittest.main()
