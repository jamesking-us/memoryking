"""Private files, process ownership, authenticated tokens and durable control state."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import os
import secrets
import stat
from pathlib import Path
from typing import Any

from memoryking.contracts.canonical import canonical_bytes, load_strict
from memoryking.domain.local import LocalError

# Largest signed journal line accepted on write and read. It covers every journaled
# action (bindings are bounded at 128 KiB canonical, selectors at 16 KiB) with headroom.
JOURNAL_RECORD_LIMIT = 262144


def sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
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
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


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


class Control:
    """Not part of content backups. Same-UID filesystem compromise is out of scope."""

    def __init__(self, path: Path):
        self.path = path
        private_file(path / "identity.json")
        identity = load_strict((path / "identity.json").read_bytes())
        self.store: str = identity["store"]
        self.key = bytes.fromhex(identity["key"])
        # Verified journal prefix: (byte length, running sha256, records, previous line hash).
        self._verified: tuple[int, Any, list[dict[str, Any]], str] | None = None
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

    def journal(self, anchor: dict[str, Any]) -> list[dict[str, Any]]:
        """Verify the hash-chained journal; only an unanchored torn final line is tolerated."""
        private_file(self.path / "journal")
        data = (self.path / "journal").read_bytes()
        records: list[dict[str, Any]] = []
        previous = "0" * 64
        position = 0
        cached = self._verified
        if (
            cached is not None
            and cached[0] <= len(data)
            and hashlib.sha256(data[: cached[0]]).digest() == cached[1].digest()
        ):
            position, records, previous = cached[0], list(cached[2]), cached[3]
        self.torn_tail = None
        while position < len(data):
            end = data.find(b"\n", position)
            line = data[position : end if end >= 0 else len(data)]
            record: dict[str, Any] | None = None
            failure: LocalError | None = None
            try:
                record = self._verify(line.decode("ascii"), JOURNAL_RECORD_LIMIT)
                if record["previous"] != previous or record["sequence"] != len(records) + 1:
                    raise LocalError("journal_invalid")
            except UnicodeError:
                failure = LocalError("invalid_token")
            except LocalError as exc:
                failure = exc
            if failure is not None or end < 0:
                # A crash while appending leaves a final line without its newline. It is
                # discardable only when every anchored record before it verified; a
                # complete line that fails, or damage anywhere else, stays fatal.
                if end < 0 and len(records) >= anchor["journal_sequence"]:
                    self.torn_tail = position
                    break
                raise failure or LocalError("journal_invalid")
            assert record is not None
            previous = hashlib.sha256(line).hexdigest()
            records.append(record)
            position = end + 1
        verified_end = position if self.torn_tail is None else self.torn_tail
        self._verified = (
            verified_end,
            hashlib.sha256(data[:verified_end]),
            list(records),
            previous,
        )
        index = anchor["journal_sequence"]
        expected = (
            "0" * 64
            if index == 0
            else (
                hashlib.sha256(self.sign(records[index - 1]).encode()).hexdigest()
                if index <= len(records)
                else "missing"
            )
        )
        if expected != anchor["journal_head"]:
            raise LocalError("journal_rollback")
        return list(records)

    def append(self, anchor: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
        records = self.journal(anchor)
        previous = (
            "0" * 64 if not records else hashlib.sha256(self.sign(records[-1]).encode()).hexdigest()
        )
        record = {"sequence": len(records) + 1, "previous": previous, "action": action}
        token = self.sign(record)
        line = token.encode() + b"\n"
        if len(token) > JOURNAL_RECORD_LIMIT:
            # Refused before any byte is written, so the store stays healthy and readable.
            raise LocalError("journal_record_limit")
        verified = self._verified
        assert verified is not None
        with (self.path / "journal").open("r+b") as stream:
            if self.torn_tail is not None:
                stream.truncate(self.torn_tail)
                self.torn_tail = None
            stream.seek(0, os.SEEK_END)
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        prefix = verified[1].copy()
        prefix.update(line)
        self._verified = (
            verified[0] + len(line),
            prefix,
            [*verified[2], record],
            hashlib.sha256(token.encode()).hexdigest(),
        )
        anchor["journal_sequence"] = record["sequence"]
        anchor["journal_head"] = hashlib.sha256(token.encode()).hexdigest()
        self.anchor(anchor)
        return record
