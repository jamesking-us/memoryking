"""Private files, process ownership, authenticated tokens and durable control state."""

from __future__ import annotations

import base64
import errno
import fcntl
import hashlib
import hmac
import os
import secrets
import stat
import threading
import time
from array import array
from pathlib import Path
from typing import Any

from memoryking.contracts.canonical import canonical_bytes, load_strict
from memoryking.domain.local import LocalError

# Largest signed journal line accepted on write and read. It covers every journaled
# action (bindings are bounded at 128 KiB canonical, selectors at 16 KiB) with headroom.
JOURNAL_RECORD_LIMIT = 262144
# The last record that could not be applied (sequence, kind, tenant, error code), beside the journal.
APPLY_FAILURE = "journal-apply-failure"


def durable_sync(fd: int) -> None:
    """Flush a descriptor to stable storage.

    Where the platform has ``fcntl.F_FULLFSYNC`` (macOS) a plain ``fsync`` does not force
    the drive to flush its cache, so the full flush is used, matching SQLite's ``fullfsync``.
    It falls back to ``os.fsync`` only when the call is unsupported for that descriptor.
    """
    full = getattr(fcntl, "F_FULLFSYNC", None)
    if full is not None:
        try:
            fcntl.fcntl(fd, full)
            return
        except OSError as exc:
            if exc.errno not in _FULLSYNC_UNSUPPORTED:
                raise
    os.fsync(fd)


_FULLSYNC_UNSUPPORTED = {
    code
    for code in (
        getattr(errno, name, None) for name in ("ENOTSUP", "EOPNOTSUPP", "EINVAL", "ENOTTY")
    )
    if code is not None
}


def sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        durable_sync(fd)
    finally:
        os.close(fd)


def private_dir(path: Path, *, create: bool = False) -> None:
    if create and not path.exists():
        if not path.parent.exists():
            private_dir(path.parent, create=True)
        path.mkdir(mode=0o700)
        sync_dir(path.parent)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise LocalError("unsafe_permissions")


def private_file(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise LocalError("unsafe_permissions")


def atomic(path: Path, data: bytes) -> None:
    if path.exists() or path.is_symlink():
        private_file(path)
    temporary = path.with_name(".stage-" + secrets.token_hex(12))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            durable_sync(stream.fileno())
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


BLOB_INDEX = "blob-index"


def write_blob_index(directory: Path, entries: list[str]) -> dict[str, Any]:
    """Write the sorted blob list as its own file; the signed manifest carries only its hash.

    The canonical signing profile bounds a document at 1 MiB, far below a large store's blob
    list, so the manifest commits to the list by digest and count instead of embedding it.
    """
    data = "".join(entry + "\n" for entry in sorted(entries)).encode()
    atomic(directory / BLOB_INDEX, data)
    return {"sha256": hashlib.sha256(data).hexdigest(), "count": len(entries)}


def read_blob_index(directory: Path, committed: Any) -> list[str]:
    """The blob list, accepted only if it is exactly the file the signed manifest committed to."""
    if (
        not isinstance(committed, dict)
        or set(committed) != {"sha256", "count"}
        or not isinstance(committed["sha256"], str)
        or not isinstance(committed["count"], int)
    ):
        raise LocalError("backup_invalid")
    path = directory / BLOB_INDEX
    private_file(path)
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != committed["sha256"]:
        raise LocalError("backup_invalid")
    try:
        lines = data.decode().split("\n")
    except UnicodeDecodeError as exc:
        raise LocalError("backup_invalid") from exc
    entries, tail = lines[:-1], lines[-1]
    if tail != "" or len(entries) != committed["count"] or entries != sorted(set(entries)):
        raise LocalError("backup_invalid")
    return entries


class Ownership:
    def __init__(self, directory: Path):
        self.pid = os.getpid()
        path = directory / "owner.lock"
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            private_file(path)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception as exc:
            os.close(self.fd)
            raise LocalError("owner_busy_or_unsafe") from exc

    def check(self) -> None:
        if self.pid != os.getpid():
            raise LocalError("inherited_owner_forbidden")
        if self.fd < 0:
            raise LocalError("owner_closed")

    def close(self) -> None:
        if self.fd >= 0:
            fd, self.fd = self.fd, -1
            os.close(fd)


class JournalState:
    """The verified journal prefix: where it ends and the hash chain over its lines.

    `hashes` holds the 32-byte SHA-256 of each record line (without its newline) and
    `offsets` where each line starts, so a verification can resume after the last
    verified line and the anchor can be compared at any sequence without the records.
    """

    __slots__ = ("dev", "ino", "size", "mtime", "offsets", "hashes", "records")

    def __init__(self, info: os.stat_result, size: int):
        self.dev, self.ino, self.size, self.mtime = (
            info.st_dev,
            info.st_ino,
            size,
            info.st_mtime_ns,
        )
        self.offsets: array[int] = array("Q")
        self.hashes = bytearray()
        self.records: list[dict[str, Any]] | None = None


class JournalCache:
    """A verified journal prefix, optionally shared by every `Control` handed the same one."""

    def __init__(self, *, keep_records: bool = True):
        self.keep_records = keep_records
        self.lock = threading.RLock()
        self.state: JournalState | None = None


class Control:
    """Not part of content backups. Same-UID filesystem compromise is out of scope."""

    def __init__(self, path: Path, *, cache: JournalCache | None = None):
        self.path = path
        private_file(path / "identity.json")
        identity = load_strict((path / "identity.json").read_bytes())
        self.store: str = identity["store"]
        self.key = bytes.fromhex(identity["key"])
        self._cache = cache or JournalCache(keep_records=True)
        # Byte offset of an unacknowledged torn final line, discarded on the next append.
        self.torn_tail: int | None = None

    def sign(self, body: dict[str, Any]) -> str:
        raw = canonical_bytes(body)
        return (
            base64.urlsafe_b64encode(raw).decode().rstrip("=")
            + "."
            + hmac.new(self.key, raw, hashlib.sha256).hexdigest()
        )

    def verify(self, token: str) -> dict[str, Any]:
        return self._verify(token, 8192)

    def verify_document(self, path: Path) -> dict[str, Any]:
        """Larger signed operator archives; never accepts an HTTP credential."""
        private_file(path)
        limit = 16 * 1024 * 1024
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        try:
            return self._verify(raw.decode("utf-8"), limit)
        except UnicodeError as exc:
            raise LocalError("invalid_token") from exc

    def _verify(self, token: str, limit: int) -> dict[str, Any]:
        try:
            if len(token) > limit:
                raise ValueError
            encoded, digest = token.split(".")
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            if not hmac.compare_digest(hmac.new(self.key, raw, hashlib.sha256).hexdigest(), digest):
                raise ValueError
            body = load_strict(raw)
            if not isinstance(body, dict):
                raise ValueError
            return body
        except Exception as exc:
            raise LocalError("invalid_token") from exc

    def read_anchor(self) -> dict[str, Any]:
        try:
            private_file(self.path / "anchor")
            return self.verify((self.path / "anchor").read_text())
        except Exception as exc:
            raise LocalError("recovery_required") from exc

    def anchor(self, value: dict[str, Any]) -> None:
        atomic(self.path / "anchor", self.sign(value).encode())

    def _usable(self, stream: Any, info: os.stat_result, state: JournalState | None) -> bool:
        """Whether the cached verified prefix may be extended instead of re-verified.

        The file must be the same one and not shorter, an unchanged-size file must not have
        been written since, and the last verified line must still hash to the cached chain
        head. A signed record names the hash of its predecessor, so a matching head binds the
        cached prefix; damage elsewhere is found by the next full verification, never used.
        """
        if (
            state is None
            or (info.st_dev, info.st_ino) != (state.dev, state.ino)
            or info.st_size < state.size
            or (info.st_size == state.size and info.st_mtime_ns != state.mtime)
        ):
            return False
        if state.offsets:
            stream.seek(state.offsets[-1])
            tail = stream.read(state.size - state.offsets[-1])
            if not tail.endswith(b"\n") or not hmac.compare_digest(
                hashlib.sha256(tail[:-1]).digest(), bytes(state.hashes[-32:])
            ):
                return False
        return True

    def _sync(self, anchor: dict[str, Any]) -> JournalState:
        """Verify the journal, reading and hashing only what the cache has not verified.

        Only an unanchored torn final line is tolerated. The anchor's sequence and head are
        compared on every call, so a truncated, rolled-back or replaced journal is refused
        whether or not the cache could be extended.
        """
        path = self.path / "journal"
        private_file(path)
        with self._cache.lock, path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            cached = self._cache.state
            resume = cached if self._usable(stream, info, cached) else None
            if resume is None:
                position, previous, count = 0, "0" * 64, 0
            else:
                position, count = resume.size, len(resume.offsets)
                previous = bytes(resume.hashes[-32:]).hex() if count else "0" * 64
            stream.seek(position)
            data = stream.read()
            info = os.fstat(stream.fileno())
            offsets: list[int] = []
            hashes = bytearray()
            records: list[dict[str, Any]] = []
            self.torn_tail = None
            cursor = 0
            while cursor < len(data):
                end = data.find(b"\n", cursor)
                line = data[cursor : end if end >= 0 else len(data)]
                record: dict[str, Any] | None = None
                failure: LocalError | None = None
                try:
                    record = self._verify(line.decode("ascii"), JOURNAL_RECORD_LIMIT)
                    if record["previous"] != previous or record["sequence"] != count + 1:
                        raise LocalError("journal_invalid")
                except UnicodeError:
                    failure = LocalError("invalid_token")
                except LocalError as exc:
                    failure = exc
                if failure is not None or end < 0:
                    # A crash while appending leaves a final line without its newline. It is
                    # discardable only when every anchored record before it verified; a
                    # complete line that fails, or damage anywhere else, stays fatal.
                    if end < 0 and count >= anchor["journal_sequence"]:
                        self.torn_tail = position + cursor
                        break
                    raise failure or LocalError("journal_invalid")
                assert record is not None
                digest = hashlib.sha256(line).digest()
                previous = digest.hex()
                offsets.append(position + cursor)
                hashes += digest
                if self._cache.keep_records:
                    records.append(record)
                count += 1
                cursor = end + 1
            verified_end = position + cursor
            if resume is None:
                state = JournalState(info, verified_end)
                state.records = [] if self._cache.keep_records else None
            else:
                state = resume
                state.size, state.mtime = verified_end, info.st_mtime_ns
            state.offsets.extend(offsets)
            state.hashes += hashes
            if state.records is not None:
                state.records.extend(records)
            self._check_anchor(state, anchor)
            self._cache.state = state
            return state

    def _check_anchor(self, state: JournalState, anchor: dict[str, Any]) -> None:
        index = anchor["journal_sequence"]
        if index == 0:
            expected = "0" * 64
        elif index <= len(state.offsets):
            expected = bytes(state.hashes[(index - 1) * 32 : index * 32]).hex()
        else:
            expected = "missing"
        if not hmac.compare_digest(expected.encode(), anchor["journal_head"].encode()):
            raise LocalError("journal_rollback")

    def head(self, count: int) -> str:
        """The chain head (hash of the record line) after `count` verified records."""
        state = self._cache.state
        if count == 0:
            return "0" * 64
        if state is None or count > len(state.offsets):
            raise LocalError("journal_invalid")
        return bytes(state.hashes[(count - 1) * 32 : count * 32]).hex()

    def verified_count(self, anchor: dict[str, Any]) -> int:
        """Verify the journal against the anchor and return how many records it holds."""
        return len(self._sync(anchor).offsets)

    def records_after(self, anchor: dict[str, Any], sequence: int) -> list[dict[str, Any]]:
        """Verified records with a sequence above `sequence`, reading only that tail."""
        state = self._sync(anchor)
        if state.records is not None:
            return list(state.records[sequence:])
        if sequence >= len(state.offsets):
            return []
        with self._cache.lock, (self.path / "journal").open("rb") as stream:
            stream.seek(state.offsets[sequence])
            data = stream.read(state.size - state.offsets[sequence])
        records = []
        for index, line in enumerate(data.split(b"\n")[:-1], start=sequence):
            if not hmac.compare_digest(
                hashlib.sha256(line).digest(), bytes(state.hashes[index * 32 : index * 32 + 32])
            ):
                # The file changed under the cache between verification and this read.
                raise LocalError("journal_invalid")
            records.append(self._verify(line.decode("ascii"), JOURNAL_RECORD_LIMIT))
        return records

    def note_apply_failure(self, record: dict[str, Any], exc: BaseException) -> None:
        """Remember which record could not be applied and why, for the operator's listing.

        Diagnostic only and unsigned: it holds the sequence, kind, tenant and an error code,
        never record content or exception text, and nothing trusts it. It is rewritten only
        when the failure changes, so a record that fails on every request costs one write.
        """
        action = record.get("action", {})
        code = exc.code if isinstance(exc, LocalError) else None
        if code is None:
            state = getattr(exc, "sqlstate", None)
            code = (
                "statement_timeout"
                if state == "57014"
                else f"database_error_{state}"
                if isinstance(state, str) and state.isalnum()
                else type(exc).__name__
            )
        detail = {
            "sequence": record["sequence"],
            "kind": action.get("kind"),
            "tenant": action.get("tenant"),
            "error": code,
        }
        try:
            existing = self.apply_failure()
            if existing is not None and all(existing.get(k) == v for k, v in detail.items()):
                return
            atomic(
                self.path / APPLY_FAILURE,
                canonical_bytes({**detail, "attempted_ms": time.time_ns() // 1_000_000}),
            )
        except (OSError, LocalError):
            pass

    def apply_failure(self) -> dict[str, Any] | None:
        try:
            private_file(self.path / APPLY_FAILURE)
            value = load_strict((self.path / APPLY_FAILURE).read_bytes())
        except (OSError, LocalError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def clear_apply_failure(self, applied: int) -> None:
        """Forget a recorded failure once that record, or a later one, has been applied."""
        held = self.apply_failure()
        if (
            held is not None
            and isinstance(held.get("sequence"), int)
            and held["sequence"] <= applied
        ):
            (self.path / APPLY_FAILURE).unlink(missing_ok=True)

    def describe_pending(
        self, records: list[dict[str, Any]], applied: int, held: int
    ) -> dict[str, Any]:
        """Unapplied records for an operator: sequence, kind, tenant and the last apply error.

        Record content is never included: a record may carry a binding, a selector or a
        principal that is itself sensitive.
        """
        failure = self.apply_failure()
        pending = []
        for record in records:
            action = record["action"]
            last = None
            if failure is not None and failure.get("sequence") == record["sequence"]:
                last = {"error": failure.get("error"), "attempted_ms": failure.get("attempted_ms")}
            pending.append(
                {
                    "sequence": record["sequence"],
                    "kind": action.get("kind"),
                    "tenant": action.get("tenant"),
                    "last_apply_error": last,
                }
            )
        return {"journal_records": held, "applied": applied, "pending": pending}

    def journal(self, anchor: dict[str, Any]) -> list[dict[str, Any]]:
        """The verified, hash-chained journal; only an unanchored torn final line is tolerated."""
        return self.records_after(anchor, 0)

    def append(self, anchor: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
        state = self._sync(anchor)
        count = len(state.offsets)
        previous = bytes(state.hashes[-32:]).hex() if count else "0" * 64
        record = {"sequence": count + 1, "previous": previous, "action": action}
        token = self.sign(record)
        line = token.encode() + b"\n"
        if len(token) > JOURNAL_RECORD_LIMIT:
            # Refused before any byte is written, so the store stays healthy and readable.
            raise LocalError("journal_record_limit")
        with self._cache.lock, (self.path / "journal").open("r+b") as stream:
            if self.torn_tail is not None:
                stream.truncate(self.torn_tail)
                self.torn_tail = None
            if stream.seek(0, os.SEEK_END) != state.size:
                # Another writer moved the file after verification; write nothing.
                raise LocalError("journal_invalid")
            stream.write(line)
            stream.flush()
            durable_sync(stream.fileno())
            info = os.fstat(stream.fileno())
        digest = hashlib.sha256(token.encode()).digest()
        state.offsets.append(state.size)
        state.hashes += digest
        if state.records is not None:
            state.records.append(record)
        state.size += len(line)
        state.mtime = info.st_mtime_ns
        anchor["journal_sequence"] = record["sequence"]
        anchor["journal_head"] = digest.hex()
        self.anchor(anchor)
        return record
