"""Sharded blobs, incremental backups taken without the store lock, and direct blob deletion."""

import hashlib
import os
import pathlib
import threading
import unittest
from unittest import mock

from test_journal import observation
from test_scale_recovery import ScaleCase

from memoryking.adapters import blobs as blobfiles
from memoryking.adapters.local_files import atomic, write_blob_index
from memoryking.adapters.local_store import LocalStore
from memoryking.domain.local import LocalError


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class BlobCases(ScaleCase):
    def blob_files(self, directory=None):
        return {n: p for n, p in blobfiles.iterate(directory or self.root / "blobs" / "main")}

    def texts(self):
        """The text of every live observation, read through the blob lookup both layouts use."""
        rows = self.store.db.execute(
            "SELECT tenant,blob FROM observations WHERE deleted=0 AND blob IS NOT NULL"
        ).fetchall()
        return {blobfiles.blob_path(self.root, t, b).read_text() for t, b in rows}

    def make_flat(self):
        """Put every blob back in the layout stores had before sharding."""
        directory = self.root / "blobs" / "main"
        for name, path in list(self.blob_files(directory).items()):
            if path.parent != directory:
                os.rename(path, directory / name)
        for child in directory.iterdir():
            if child.is_dir():
                child.rmdir()

    def test_new_blobs_are_sharded_by_hash_prefix_and_read_back(self):
        self.capture(0, "sharded body")
        name = digest("sharded body")
        self.assertTrue((self.root / "blobs" / "main" / name[:2] / name).is_file())
        self.assertEqual({"sharded body"}, self.texts())

    def test_a_flat_store_reads_and_migrates_in_interruptible_steps(self):
        for number in range(5):
            self.capture(number, f"body {number}")
        self.make_flat()
        directory = self.root / "blobs" / "main"
        self.assertEqual(5, len([p for p in directory.iterdir() if p.is_file()]))
        # Old paths stay readable before anything moves.
        texts = self.texts()
        self.assertEqual({f"body {n}" for n in range(5)}, texts)
        # An interrupted migration (two files, then stop) leaves both layouts, all readable.
        self.assertFalse(blobfiles.migrate(self.root, budget_s=10, limit=2))
        self.assertEqual(3, len([p for p in directory.iterdir() if p.is_file()]))
        texts = self.texts()
        self.assertEqual({f"body {n}" for n in range(5)}, texts)
        self.assertTrue(blobfiles.migrate(self.root))
        self.assertEqual([], [p for p in directory.iterdir() if p.is_file()])
        self.assertEqual(5, len(self.blob_files()))
        self.assertTrue(blobfiles.migrate(self.root))  # idempotent

    def test_housekeeping_moves_flat_blobs_without_a_request(self):
        for number in range(3):
            self.capture(number, f"body {number}")
        self.make_flat()
        self.store.housekeeping(interval_ms=10**12)
        directory = self.root / "blobs" / "main"
        self.assertEqual([], [p for p in directory.iterdir() if p.is_file()])

    def test_a_second_backup_links_blobs_the_first_holds_and_copies_only_new_ones(self):
        for number in range(3):
            self.capture(number, f"body {number}")
        first = self.base / "backup-a"
        self.store.backup(first)
        self.capture(10, "a newer body")
        second = self.base / "backup-b"
        manifest = self.store.backup(second, previous=first)
        self.assertEqual((3, 1), (manifest["blobs_linked"], manifest["blobs_copied"]))
        name = digest("body 0")
        a, b = (d / "blobs" / "main" / name[:2] / name for d in (first, second))
        self.assertEqual(a.stat().st_ino, b.stat().st_ino)
        # Each backup is complete on its own: removing the first leaves the second restorable.
        import shutil

        shutil.rmtree(first)
        self.store.close()
        self.store = None
        LocalStore.restore(self.root, self.control, second, clock=self.clock)
        self.store = LocalStore(self.root, self.control, clock=self.clock)
        texts = self.texts()
        self.assertEqual({"body 0", "body 1", "body 2", "a newer body"}, texts)

    def test_a_previous_copy_is_verified_by_hash_not_trusted_by_name(self):
        for number in range(2):
            self.capture(number, f"body {number}")
        first = self.base / "backup-a"
        self.store.backup(first)
        name = digest("body 0")
        damaged = first / "blobs" / "main" / name[:2] / name
        damaged.unlink()
        atomic(damaged, b"not the content")
        manifest = self.store.backup(self.base / "backup-b", previous=first)
        self.assertEqual((1, 1), (manifest["blobs_linked"], manifest["blobs_copied"]))
        good = self.base / "backup-b" / "blobs" / "main" / name[:2] / name
        self.assertEqual(name, blobfiles.sha256_file(good))
        self.assertNotEqual(damaged.stat().st_ino, good.stat().st_ino)

    def test_an_untrusted_previous_backup_means_a_full_copy(self):
        self.capture(0, "body 0")
        first = self.base / "backup-a"
        self.store.backup(first)
        (first / "manifest").write_bytes(b"tampered")
        os.chmod(first / "manifest", 0o600)
        manifest = self.store.backup(self.base / "backup-b", previous=first)
        self.assertEqual((0, 1), (manifest["blobs_linked"], manifest["blobs_copied"]))
        manifest = self.store.backup(self.base / "backup-c", previous=self.base / "missing")
        self.assertEqual((0, 1), (manifest["blobs_linked"], manifest["blobs_copied"]))

    def test_requests_proceed_while_blob_files_are_copied(self):
        for number in range(3):
            self.capture(number, f"body {number}")
        outcomes = []
        real = blobfiles.copy_verified

        def probe(source, destination, expected):
            def attempt():
                outcomes.append(self.store.lock.acquire(timeout=2))
                if outcomes[-1]:
                    self.store.lock.release()

            thread = threading.Thread(target=attempt)
            thread.start()
            thread.join(5)
            return real(source, destination, expected)

        with mock.patch.object(blobfiles, "copy_verified", probe):
            self.store.backup(self.base / "backup-a")
        self.assertEqual([True, True, True], outcomes)

    def test_backup_and_restore_hash_the_database_in_blocks(self):
        self.capture(0, "body 0")
        backup = self.base / "backup-a"
        real = pathlib.Path.read_bytes

        def guarded(path):
            if path.name == "store.sqlite":
                raise AssertionError("the database file must be streamed, not read whole")
            return real(path)

        with mock.patch.object(pathlib.Path, "read_bytes", guarded):
            self.store.backup(backup)
            self.store.close()
            self.store = None
            LocalStore.restore(self.root, self.control, backup, clock=self.clock)
        self.store = LocalStore(self.root, self.control, clock=self.clock)

    def test_restore_accepts_a_backup_that_recorded_flat_blob_paths(self):
        for number in range(2):
            self.capture(number, f"body {number}")
        backup = self.base / "backup-a"
        manifest = self.store.backup(backup)
        # Rewrite the backup the way releases before sharding wrote it.
        entries = []
        for path in sorted((backup / "blobs" / "main").rglob("*")):
            if path.is_file():
                flat = backup / "blobs" / "main" / path.name
                os.rename(path, flat)
                entries.append(f"blobs/main/{path.name}")
        for shard in (backup / "blobs" / "main").iterdir():
            if shard.is_dir():
                shard.rmdir()
        manifest["blob_index"] = write_blob_index(backup, entries)
        atomic(backup / "manifest", self.store.control.sign(manifest).encode())
        self.store.close()
        self.store = None
        LocalStore.restore(self.root, self.control, backup, clock=self.clock)
        self.store = LocalStore(self.root, self.control, clock=self.clock)
        texts = self.texts()
        self.assertEqual({"body 0", "body 1"}, texts)

    def test_a_blob_purged_during_the_copy_is_left_out_and_a_missing_live_blob_is_refused(self):
        first = self.capture(0, "body 0")
        self.capture(1, "body 1")
        real = blobfiles.copy_verified
        done = []

        def purge_then_copy(source, destination, expected):
            if not done:
                done.append(True)
                self.store.purge(self.credential, first["observation"])
            return real(source, destination, expected)

        with mock.patch.object(blobfiles, "copy_verified", purge_then_copy):
            manifest = self.store.backup(self.base / "backup-a")
        self.assertEqual(1, manifest["blob_index"]["count"])
        self.store.close()
        self.store = None
        LocalStore.restore(self.root, self.control, self.base / "backup-a", clock=self.clock)
        self.store = LocalStore(self.root, self.control, clock=self.clock)
        texts = self.texts()
        self.assertEqual({"body 1"}, texts)
        # A live blob that is simply gone is corruption, not a purge.
        name = digest("body 1")
        (self.root / "blobs" / "main" / name[:2] / name).unlink()
        with self.assertRaises(LocalError) as raised:
            self.store.backup(self.base / "backup-b")
        self.assertEqual("blob_corrupt", raised.exception.code)

    def test_purge_deletes_its_own_file_and_never_sweeps_the_directory(self):
        a = self.capture(0, "shared text")
        self.store.capture(
            self.credential, self.namespace, "k-other", observation(1, "shared text")
        )
        self.capture(2, "unique text")
        orphan = self.root / "blobs" / "main" / "ab" / ("ab" + "0" * 62)
        orphan.parent.mkdir(exist_ok=True)
        atomic(orphan, b"left behind by a crash")
        name = digest("shared text")
        shared = self.root / "blobs" / "main" / name[:2] / name
        with mock.patch.object(LocalStore, "gc", side_effect=AssertionError("no sweep on purge")):
            self.store.purge(self.credential, a["observation"])
            # Another observation names the same text, so its file stays.
            self.assertTrue(shared.is_file())
            other = self.store.db.execute(
                "SELECT id FROM observations WHERE blob=? AND deleted=0", (name,)
            ).fetchone()[0]
            self.store.purge(self.credential, other)
        self.assertFalse(shared.exists())
        self.assertTrue(orphan.exists(), "an unrelated orphan is the explicit sweep's business")
        self.assertEqual(1, self.store.gc())
        self.assertFalse(orphan.exists())

    def test_a_purge_that_crashed_before_deleting_its_file_is_swept_at_the_next_start(self):
        made = self.capture(0, "doomed text")
        name = digest("doomed text")
        file = self.root / "blobs" / "main" / name[:2] / name
        with mock.patch.object(
            LocalStore, "_delete_unreferenced", side_effect=RuntimeError("crash")
        ):
            with self.assertRaises(RuntimeError):
                self.store.purge(self.credential, made["observation"])
        self.assertTrue(file.exists())
        self.store.close()
        self.store = LocalStore(self.root, self.control, clock=self.clock)
        self.assertFalse(file.exists())
        # A normal start does not sweep.
        orphan = self.root / "blobs" / "main" / "cd" / ("cd" + "1" * 62)
        orphan.parent.mkdir(exist_ok=True)
        atomic(orphan, b"x")
        self.store.close()
        self.store = LocalStore(self.root, self.control, clock=self.clock)
        self.assertTrue(orphan.exists())


if __name__ == "__main__":
    unittest.main()
