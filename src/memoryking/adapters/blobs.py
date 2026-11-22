"""Content-addressed blob files: the sharded layout, streaming hashes and durable copies.

A blob is named by the SHA-256 of its bytes and never changes, so any copy of it can be
verified by hashing and shared between backups. New blobs live at
`blobs/<tenant>/<first two hex digits>/<digest>`; stores written before sharding keep every blob
directly under `blobs/<tenant>/`. Both layouts are readable at all times: `locate` finds a blob in
either, and `migrate` moves flat files into their shard one atomic rename at a time, so an
interrupted migration leaves a store that still reads and can simply be continued.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from memoryking.adapters.local_files import (
    atomic,
    durable_sync,
    private_dir,
    private_file,
    sync_dir,
)
from memoryking.domain.local import LocalError

SHARD = 2
BLOCK = 1 << 20
DIGEST = re.compile(r"[a-f0-9]{64}")
SHARD_NAME = re.compile(r"[a-f0-9]{2}")


def sha256_file(path: Path) -> str:
    """Hash a file in blocks; never holds the whole file in memory."""
    private_file(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(BLOCK):
            digest.update(block)
    return digest.hexdigest()


def sharded(directory: Path, digest: str) -> Path:
    return directory / digest[:SHARD] / digest


def locate(directory: Path, digest: str) -> Path:
    """The blob's current file in either layout; the sharded path when it exists in neither
    (so a write lands in the new layout). A migration rename between the two checks is
    retried once."""
    candidates = (sharded(directory, digest), directory / digest)
    for _ in range(2):
        for path in candidates:
            if path.is_file():
                return path
    return candidates[0]


def blob_path(root: Path, tenant: str, digest: str) -> Path:
    return locate(root / "blobs" / tenant, digest)


def relative(tenant: str, digest: str) -> str:
    """The sharded path a backup records for a blob."""
    return f"blobs/{tenant}/{digest[:SHARD]}/{digest}"


def valid_relative(entry: str) -> bool:
    """A backup's blob path in either layout (flat is what earlier backups recorded)."""
    parts = entry.split("/")
    if parts[0] != "blobs" or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,96}", parts[1] if len(parts) > 1 else ""
    ):
        return False
    if len(parts) == 3:
        return bool(DIGEST.fullmatch(parts[2]))
    return (
        len(parts) == 4
        and bool(SHARD_NAME.fullmatch(parts[2]))
        and bool(DIGEST.fullmatch(parts[3]))
        and parts[3].startswith(parts[2])
    )


def digest_of(entry: str) -> str:
    return entry.rsplit("/", 1)[1]


def iterate(directory: Path) -> Iterator[tuple[str, Path]]:
    """Every blob file under a tenant's directory, in either layout."""
    if not directory.is_dir():
        return
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False) and SHARD_NAME.fullmatch(entry.name):
                with os.scandir(entry.path) as inner:
                    for item in inner:
                        yield item.name, Path(item.path)
            else:
                yield entry.name, Path(entry.path)


def write_durable(path: Path, stream_in: object, expected: str | None = None) -> str:
    """Copy a readable binary stream to `path` through a staging file: one fsync of the file,
    then an atomic rename. The directory is NOT synced here (the caller syncs each directory
    once after a batch). Returns the SHA-256 of what was written; with `expected`, a mismatch
    removes the staging file and raises `blob_corrupt`."""
    temporary = path.with_name(".stage-" + secrets.token_hex(12))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    digest = hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as out:
            while block := stream_in.read(BLOCK):  # type: ignore[attr-defined]
                digest.update(block)
                out.write(block)
            out.flush()
            durable_sync(out.fileno())
        if expected is not None and digest.hexdigest() != expected:
            raise LocalError("blob_corrupt")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return digest.hexdigest()


def copy_verified(source: Path, destination: Path, digest: str) -> int:
    """Copy one blob, hashing while streaming; the copy must hash to `digest`. Returns bytes."""
    private_file(source)
    with source.open("rb") as stream:
        write_durable(destination, stream, digest)
    return destination.stat().st_size


def link_or_copy(previous: Path, source: Path, destination: Path, digest: str) -> tuple[str, int]:
    """Put a blob into a new backup: hard-link the previous backup's copy when that copy
    hashes to `digest` (verified by reading it, never trusted by name), else copy from the
    live store. Returns ("linked" | "copied", bytes)."""
    try:
        if sha256_file(previous) == digest:
            try:
                os.link(previous, destination)
                return "linked", destination.stat().st_size
            except OSError:
                pass  # another filesystem, or no hard links: copy instead
    except (OSError, LocalError):
        pass
    return "copied", copy_verified(source, destination, digest)


def sync_directories(paths: set[Path]) -> None:
    for path in sorted(paths):
        sync_dir(path)


def shard_directory(directory: Path, digest: str) -> Path:
    target = directory / digest[:SHARD]
    private_dir(target, create=True)
    return target


def migrate(
    root: Path, budget_s: float = 0.2, limit: int = 20000, tenant: str | None = None
) -> bool:
    """Move flat blobs into their shard directories until the budget is spent.

    Each move is one atomic rename; nothing is copied or deleted, so an interrupted run loses
    nothing and readers find the file in either place. A flat file that duplicates an existing
    sharded one is removed only after both hash the same. Returns True when no flat blob is
    left under any tenant.
    """
    started, moved = time.monotonic(), 0
    blobs = root / "blobs"
    if not blobs.is_dir():
        return True
    touched: set[Path] = set()
    done = True
    for tenant_path in sorted(blobs.iterdir()):
        if not tenant_path.is_dir() or (tenant is not None and tenant_path.name != tenant):
            continue
        with os.scandir(tenant_path) as entries:
            for entry in entries:
                if not (entry.is_file(follow_symlinks=False) and DIGEST.fullmatch(entry.name)):
                    continue
                if moved >= limit or time.monotonic() - started >= budget_s:
                    done = False
                    break
                target = shard_directory(tenant_path, entry.name) / entry.name
                source = Path(entry.path)
                if target.exists():
                    if sha256_file(target) == sha256_file(source):
                        source.unlink()
                else:
                    os.rename(source, target)
                touched.add(target.parent)
                moved += 1
        if not done:
            break
        touched.add(tenant_path)
    sync_directories(touched)
    return done


def link_previous(previous: Path, destination: Path, digest: str) -> int | None:
    """Hard-link an earlier backup's copy of a blob into a new backup when that copy hashes to
    `digest` (verified by reading it, never trusted by name). Returns the size, or None when
    it cannot be linked (a different filesystem, no hard links, a copy that does not verify)."""
    try:
        if sha256_file(previous) == digest:
            os.link(previous, destination)
            return destination.stat().st_size
    except (OSError, LocalError):
        pass
    return None


class FileBlobStore:
    """The blob store on a local filesystem: `blobs/<tenant>/<shard>/<digest>` under a root.

    Implements `memoryking.application.blob_store.BlobStore`."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._touched: set[Path] = set()
        self._guard = threading.Lock()

    def _directory(self, tenant: str) -> Path:
        return self.root / "blobs" / tenant

    def _remember(self, *paths: Path) -> None:
        with self._guard:
            self._touched.update(paths)

    def put(self, tenant: str, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        directory = self._directory(tenant)
        # Creating the directory syncs its parent; an existing one has nothing new to sync.
        private_dir(directory, create=True)
        path = locate(directory, digest)
        if not path.exists():
            if not path.parent.exists():
                private_dir(path.parent, create=True)
                sync_dir(directory)
            atomic(path, content)
        elif sha256_file(path) != digest:
            raise LocalError("blob_corrupt")
        return digest

    def put_stream(self, tenant: str, digest: str, stream: BinaryIO) -> None:
        directory = self._directory(tenant)
        private_dir(directory, create=True)
        shard = shard_directory(directory, digest)
        write_durable(shard / digest, stream, digest)
        (directory / digest).unlink(missing_ok=True)  # a flat copy is now redundant
        self._remember(shard, directory)

    def get(self, tenant: str, digest: str) -> bytes:
        path = blob_path(self.root, tenant, digest)
        private_file(path)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise LocalError("blob_corrupt")
        return raw

    def head(self, tenant: str, digest: str, size: int) -> bytes:
        path = blob_path(self.root, tenant, digest)
        private_file(path)
        with path.open("rb") as stream:
            return stream.read(size)

    def delete(self, tenant: str, digest: str) -> int:
        directory = self._directory(tenant)
        removed = 0
        for path in (sharded(directory, digest), directory / digest):
            if path.is_file() or path.is_symlink():
                private_file(path)
                path.unlink()
                if path.parent != directory:
                    try:
                        path.parent.rmdir()  # an emptied shard directory goes too
                    except OSError:
                        pass
                self._remember(directory if not path.parent.exists() else path.parent)
                removed = 1
        return removed

    def digests(self, tenant: str) -> list[str]:
        directory = self._directory(tenant)
        if not directory.is_dir():
            return []
        private_dir(directory)
        names = []
        for name, path in iterate(directory):
            private_file(path)
            names.append(name)
        return names

    def tenants(self) -> list[str]:
        directory = self.root / "blobs"
        return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []

    def copy_to(self, tenant: str, digest: str, destination: Path) -> int:
        return copy_verified(blob_path(self.root, tenant, digest), destination, digest)

    def sync(self) -> None:
        with self._guard:
            touched, self._touched = self._touched, set()
        sync_directories(touched)

    def maintain(self, budget_s: float = 0.2, tenant: str | None = None) -> bool:
        return migrate(self.root, budget_s=budget_s, tenant=tenant)
