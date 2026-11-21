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
import time
from collections.abc import Iterator
from pathlib import Path

from memoryking.adapters.local_files import durable_sync, private_dir, private_file, sync_dir
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


def remove(root: Path, tenant: str, digest: str) -> int:
    """Delete a blob's file wherever it lives; the directory entry is synced. Returns 0 or 1."""
    directory = root / "blobs" / tenant
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
            sync_dir(directory if not path.parent.exists() else path.parent)
            removed = 1
    return removed
