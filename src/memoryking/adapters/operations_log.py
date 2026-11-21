"""The operations log: operation intents and transitions, beside the control journal.

Every operation record is signed and names the hash of its predecessor, like a journal
record, but lives in `operations/NNNNNNNN.log` segments rolled by size. A sealed segment
ends with a signed trailer naming the head before it, its record count and its sequence
range. The anchor carries `operations_sequence` and `operations_head`, checked on every
use exactly as the journal head is, so a rolled-back or truncated log fails closed.

Compaction drops only whole sealed segments, and only once the caller has shown that every
operation in them is resolved and reconciled and that the oldest retained backup already
holds them. Before a segment is dropped, the identities it consumed are written to a
hash-committed index that a signed `base` file names, so an identity is never reused. The
base also carries the sequence and head the retained log continues from.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from memoryking.domain.local import LocalError

if TYPE_CHECKING:
    from memoryking.adapters.local_files import Control

ZERO = "0" * 64
OPERATIONS_DIR = "operations"
BASE = "base"
SEGMENT = re.compile(r"^(\d{8})\.log$")
CONSUMED = re.compile(r"^consumed-([a-f0-9]{64})$")
# A segment is sealed once it reaches this size; the next record opens a new one.
SEGMENT_LIMIT = 4 * 1024 * 1024
RECORD_LIMIT = 262144


class Segment:
    """What verification learned about one segment file."""

    __slots__ = (
        "number",
        "dev",
        "ino",
        "size",
        "mtime",
        "first",
        "last",
        "before",
        "offset",
        "sealed",
    )

    def __init__(self, number: int, info: os.stat_result, before: str):
        self.number = number
        self.dev, self.ino, self.size, self.mtime = info.st_dev, info.st_ino, 0, info.st_mtime_ns
        self.first = self.last = 0  # sequence range; last == 0 while empty
        self.before = before  # chain head before this segment's first record
        self.offset = 0  # where the last record line starts
        self.sealed = False


class OperationsState:
    __slots__ = ("base_key", "through", "head_at_base", "segments", "hashes", "count", "head")

    def __init__(self, base_key: str, through: int, head_at_base: str):
        self.base_key, self.through, self.head_at_base = base_key, through, head_at_base
        self.segments: list[Segment] = []
        self.hashes = bytearray()  # one 32-byte line hash per record after the base
        self.count = through
        self.head = head_at_base


class OperationsCache:
    """Verified operations log state and the consumed-identity set, per control tree."""

    def __init__(self) -> None:
        self.state: OperationsState | None = None
        self.consumed_key: str | None = None
        self.consumed: set[tuple[str, str]] = set()


def sync_directory(path: Path) -> None:
    from memoryking.adapters.local_files import sync_dir

    sync_dir(path)


class OperationsLog:
    def __init__(self, control: "Control", cache: OperationsCache):
        self.control = control
        self.cache = cache
        self.dir = control.path / OPERATIONS_DIR
        # Where an unacknowledged torn final line starts: (segment number, byte offset).
        self.torn_tail: tuple[int, int] | None = None

    # -- base and consumed identities ------------------------------------------------------

    def _base(self) -> tuple[dict[str, Any], str]:
        """The signed base (what a compacted prefix left behind) and its cache key."""
        from memoryking.adapters.local_files import private_file

        path = self.dir / BASE
        if not path.exists():
            return {"through_sequence": 0, "through_segment": 0, "head": ZERO, "consumed": None}, ""
        private_file(path)
        token = path.read_text()
        body = self.control.verify(token)
        if (
            not isinstance(body.get("through_sequence"), int)
            or not isinstance(body.get("through_segment"), int)
            or not isinstance(body.get("head"), str)
        ):
            raise LocalError("journal_invalid")
        return body, hashlib.sha256(token.encode()).hexdigest()

    def _load_consumed(self, base: dict[str, Any], key: str) -> None:
        if self.cache.consumed_key == key:
            return
        committed = base.get("consumed")
        identities: set[tuple[str, str]] = set()
        if committed is not None:
            from memoryking.adapters.local_files import private_file

            path = self.dir / committed["name"]
            private_file(path)
            data = path.read_bytes()
            if (
                not CONSUMED.match(committed["name"])
                or hashlib.sha256(data).hexdigest() != committed["sha256"]
                or committed["name"] != "consumed-" + committed["sha256"]
            ):
                raise LocalError("journal_invalid")
            for line in data.decode().split("\n")[:-1]:
                tenant, _, identity = line.partition(" ")
                identities.add((tenant, identity))
            if len(identities) != committed["count"]:
                raise LocalError("journal_invalid")
        self.cache.consumed, self.cache.consumed_key = identities, key

    def consumed(self, tenant: str, identity: str) -> bool:
        """Whether a compacted segment consumed this operation identity."""
        with self.control._cache.lock:
            base, key = self._base()
            self._load_consumed(base, key)
            return (tenant, identity) in self.cache.consumed

    def compacted_through(self) -> int:
        """The sequence below which segments were compacted away (0 when none were)."""
        with self.control._cache.lock:
            return int(self._base()[0]["through_sequence"])

    # -- verification ------------------------------------------------------------------------

    def _numbers(self, first: int) -> list[int]:
        if not self.dir.exists():
            return []
        found = sorted(int(m.group(1)) for n in os.listdir(self.dir) if (m := SEGMENT.match(n)))
        found = [n for n in found if n >= first]
        if found and found != list(range(found[0], found[0] + len(found))):
            raise LocalError("journal_invalid")
        if found and found[0] != first:
            raise LocalError("journal_invalid")
        return found

    def _path(self, number: int) -> Path:
        return self.dir / f"{number:08d}.log"

    def _reusable(self, state: OperationsState, numbers: list[int]) -> bool:
        """Whether the cached verified prefix may be extended instead of verified again.

        Sealed segments must be unchanged files; the last cached segment may have grown, and
        its last verified line must still hash to the cached chain head, as for the journal.
        """
        cached = state.segments
        if len(numbers) < len(cached) or numbers[: len(cached)] != [s.number for s in cached]:
            return False
        for index, seg in enumerate(cached):
            try:
                info = self._path(seg.number).stat()
            except OSError:
                return False
            if (info.st_dev, info.st_ino) != (seg.dev, seg.ino):
                return False
            if index < len(cached) - 1 or seg.sealed:
                if info.st_size != seg.size or info.st_mtime_ns != seg.mtime:
                    return False
            else:
                if info.st_size < seg.size or (
                    info.st_size == seg.size and info.st_mtime_ns != seg.mtime
                ):
                    return False
                if seg.last:
                    with self._path(seg.number).open("rb") as stream:
                        stream.seek(seg.offset)
                        tail = stream.read(seg.size - seg.offset)
                    if not tail.endswith(b"\n") or not hmac.compare_digest(
                        hashlib.sha256(tail[:-1]).digest(), bytes(state.hashes[-32:])
                    ):
                        return False
        return True

    def sync(self, anchor: dict[str, Any], *, trust_anchor: bool = False) -> OperationsState:
        """Verify the log, reading only what the cache has not verified, and check the anchor.

        Only an unanchored torn final line is tolerated. The anchor's sequence and head are
        compared on every call, so a truncated, rolled-back or replaced log is refused whether
        or not the cache could be extended.
        """
        with self.control._cache.lock:
            base, key = self._base()
            self._load_consumed(base, key)
            numbers = self._numbers(base["through_segment"] + 1)
            state = self.cache.state
            if state is not None and (state.base_key != key or not self._reusable(state, numbers)):
                state = None
            if state is None:
                state = OperationsState(key, base["through_sequence"], base["head"])
            self.torn_tail = None
            self._scan(state, numbers, anchor)
            if not trust_anchor:
                self._check_anchor(state, anchor)
            self.cache.state = state
            return state

    def _scan(self, state: OperationsState, numbers: list[int], anchor: dict[str, Any]) -> None:
        done = len(state.segments)
        resume = state.segments[-1] if state.segments and not state.segments[-1].sealed else None
        if resume is not None:
            done -= 1
        for index in range(done, len(numbers)):
            number = numbers[index]
            last_one = index == len(numbers) - 1
            path = self._path(number)
            from memoryking.adapters.local_files import private_file

            private_file(path)
            with path.open("rb") as stream:
                info = os.fstat(stream.fileno())
                seg = resume if resume is not None and resume.number == number else None
                if seg is None:
                    seg = Segment(number, info, state.head)
                    state.segments.append(seg)
                start = seg.size
                stream.seek(start)
                data = stream.read()
                info = os.fstat(stream.fileno())
            cursor = 0
            while cursor < len(data):
                end = data.find(b"\n", cursor)
                line = data[cursor : end if end >= 0 else len(data)]
                body: dict[str, Any] | None = None
                failure: LocalError | None = None
                try:
                    body = self.control._verify(line.decode("ascii"), RECORD_LIMIT)
                except UnicodeError:
                    failure = LocalError("invalid_token")
                except LocalError as exc:
                    failure = exc
                if failure is None and body is not None and "trailer" not in body:
                    if (
                        seg.sealed
                        or body.get("previous") != state.head
                        or body.get("sequence") != state.count + 1
                    ):
                        failure = LocalError("journal_invalid")
                if failure is not None or end < 0:
                    # A crash while appending leaves a final line without its newline, and
                    # only the last segment may end that way, only after every anchored record.
                    if (
                        end < 0
                        and last_one
                        and state.count >= int(anchor.get("operations_sequence", 0))
                    ):
                        self.torn_tail = (number, start + cursor)
                        break
                    raise failure or LocalError("journal_invalid")
                assert body is not None
                if "trailer" in body:
                    if (
                        seg.sealed
                        or end + 1 != len(data)
                        or not seg.last
                        or body.get("segment") != number
                        or body.get("previous_segment_head") != seg.before
                        or body.get("count") != seg.last - seg.first + 1
                        or body.get("min_sequence") != seg.first
                        or body.get("max_sequence") != seg.last
                    ):
                        raise LocalError("journal_invalid")
                    seg.sealed = True
                else:
                    digest = hashlib.sha256(line).digest()
                    state.head = digest.hex()
                    state.hashes += digest
                    state.count += 1
                    if not seg.last:
                        seg.first = state.count
                    seg.last = state.count
                    seg.offset = start + cursor
                cursor = end + 1
            seg.size, seg.mtime = start + cursor, info.st_mtime_ns
            if not last_one and not seg.sealed:
                raise LocalError("journal_invalid")
            resume = None

    def _check_anchor(self, state: OperationsState, anchor: dict[str, Any]) -> None:
        index = int(anchor.get("operations_sequence", 0))
        if index == state.through:
            expected = state.head_at_base
        elif state.through < index <= state.count:
            at = index - state.through
            expected = bytes(state.hashes[(at - 1) * 32 : at * 32]).hex()
        else:
            expected = "missing"
        if not hmac.compare_digest(
            expected.encode(), str(anchor.get("operations_head", ZERO)).encode()
        ):
            raise LocalError("journal_rollback")

    def head(self, count: int) -> str:
        """The chain head after `count` verified records (counted from the very first)."""
        state = self.cache.state
        if state is None or count > state.count or count < state.through:
            raise LocalError("journal_invalid")
        if count == state.through:
            return state.head_at_base
        return bytes(
            state.hashes[(count - state.through - 1) * 32 : (count - state.through) * 32]
        ).hex()

    def verified_count(self, anchor: dict[str, Any]) -> int:
        """Verify the log against the anchor and return the sequence of its last record."""
        return self.sync(anchor).count

    def records_after(self, anchor: dict[str, Any], sequence: int) -> list[dict[str, Any]]:
        """Verified records above `sequence`, reading only the segments holding them.

        Each is tagged `"log": "operations"` (unsigned) so the store applies it as such.
        """
        state = self.sync(anchor)
        if sequence < state.through:
            # The database is older than what compaction kept; those records are gone.
            raise LocalError("journal_invalid")
        records: list[dict[str, Any]] = []
        with self.control._cache.lock:
            for seg in state.segments:
                if seg.last and seg.last > sequence:
                    records.extend(
                        {**b, "log": "operations"}
                        for b in self._read(state, seg)
                        if b["sequence"] > sequence
                    )
        return records

    def _read(self, state: OperationsState, seg: Segment) -> list[dict[str, Any]]:
        """The records of one verified segment, each checked against the cached chain."""
        with self._path(seg.number).open("rb") as stream:
            data = stream.read(seg.size)
        records = []
        position = seg.first
        for line in data.split(b"\n")[:-1]:
            body = self.control._verify(line.decode("ascii"), RECORD_LIMIT)
            if "trailer" in body:
                continue
            at = position - state.through
            if not hmac.compare_digest(
                hashlib.sha256(line).digest(), bytes(state.hashes[(at - 1) * 32 : at * 32])
            ):
                raise LocalError("journal_invalid")
            records.append(body)
            position += 1
        return records

    # -- appending -----------------------------------------------------------------------------

    def _seal(self, state: OperationsState, seg: Segment) -> None:
        trailer = self.control.sign(
            {
                "trailer": 1,
                "segment": seg.number,
                "previous_segment_head": seg.before,
                "count": seg.last - seg.first + 1,
                "min_sequence": seg.first,
                "max_sequence": seg.last,
            }
        )
        from memoryking.adapters.local_files import durable_sync

        with self._path(seg.number).open("r+b") as stream:
            if stream.seek(0, os.SEEK_END) != seg.size:
                raise LocalError("journal_invalid")
            stream.write(trailer.encode() + b"\n")
            stream.flush()
            durable_sync(stream.fileno())
            info = os.fstat(stream.fileno())
            seg.size, seg.mtime = stream.tell(), info.st_mtime_ns
        seg.sealed = True

    def append(self, anchor: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
        from memoryking.adapters.local_files import durable_sync, private_dir

        state = self.sync(anchor)
        record = {"sequence": state.count + 1, "previous": state.head, "action": action}
        token = self.control.sign(record)
        if len(token) > RECORD_LIMIT:
            # Refused before any byte is written, so the store stays healthy and readable.
            raise LocalError("journal_record_limit")
        line = token.encode() + b"\n"
        with self.control._cache.lock:
            private_dir(self.dir, create=True)
            seg = state.segments[-1] if state.segments else None
            if seg is not None and self.torn_tail is not None and self.torn_tail[0] == seg.number:
                with self._path(seg.number).open("r+b") as stream:
                    stream.truncate(self.torn_tail[1])
                    durable_sync(stream.fileno())
                seg.size = self.torn_tail[1]
            self.torn_tail = None
            if seg is not None and not seg.sealed and seg.last and seg.size >= SEGMENT_LIMIT:
                self._seal(state, seg)
            if seg is None or seg.sealed:
                number = seg.number + 1 if seg is not None else int(self._first_number(state))
                path = self._path(number)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
                sync_directory(self.dir)
                seg = Segment(number, path.stat(), state.head)
                state.segments.append(seg)
            with self._path(seg.number).open("r+b") as stream:
                if stream.seek(0, os.SEEK_END) != seg.size:
                    # Another writer moved the file after verification; write nothing.
                    raise LocalError("journal_invalid")
                stream.write(line)
                stream.flush()
                durable_sync(stream.fileno())
                info = os.fstat(stream.fileno())
            digest = hashlib.sha256(token.encode()).digest()
            state.hashes += digest
            state.count += 1
            state.head = digest.hex()
            if not seg.last:
                seg.first = state.count
            seg.last = state.count
            seg.offset = seg.size
            seg.size += len(line)
            seg.mtime = info.st_mtime_ns
        anchor["operations_sequence"] = record["sequence"]
        anchor["operations_head"] = digest.hex()
        self.control.anchor(anchor)
        # The tag tells `StoreCore._apply` which applied position the record advances; it is
        # not part of the signed record.
        return {**record, "log": "operations"}

    def _first_number(self, state: OperationsState) -> int:
        return int(self._base()[0]["through_segment"]) + 1

    def _sweep(self) -> None:
        """Remove segments an interrupted compaction left behind its already-written base."""
        through = int(self._base()[0]["through_segment"])
        for name in os.listdir(self.dir) if self.dir.exists() else []:
            match = SEGMENT.match(name)
            if match and int(match.group(1)) <= through:
                (self.dir / name).unlink(missing_ok=True)

    # -- compaction ----------------------------------------------------------------------------

    def compact(
        self,
        anchor: dict[str, Any],
        coverage: int,
        resolved: Callable[[Iterable[tuple[str, str]]], bool],
    ) -> int:
        """Drop the oldest sealed segments that the oldest retained backup already holds.

        `coverage` is the operations position of the oldest retained backup (0 when there is
        none, which keeps everything). A segment goes only if it is sealed, lies wholly at or
        below `coverage` and the anchor, and `resolved` says every operation it names is
        resolved and reconciled. Segments go oldest first and stop at the first one that
        stays. Returns how many segments were dropped.
        """
        with self.control._cache.lock:
            state = self.sync(anchor)
            self._sweep()
            limit = min(coverage, int(anchor.get("operations_sequence", 0)))
            drop: list[Segment] = []
            names: set[tuple[str, str]] = set()
            for seg in state.segments:
                if not seg.sealed or not seg.last or seg.last > limit:
                    break
                ids = {
                    (rec["action"]["tenant"], rec["action"]["operation"])
                    for rec in self._read(state, seg)
                }
                if not resolved(ids):
                    break
                names |= ids
                drop.append(seg)
            if not drop:
                return 0
            base, key = self._base()
            self._load_consumed(base, key)
            identities = sorted(self.cache.consumed | names)
            data = "".join(f"{tenant} {identity}\n" for tenant, identity in identities).encode()
            digest = hashlib.sha256(data).hexdigest()
            from memoryking.adapters.local_files import atomic

            atomic(self.dir / ("consumed-" + digest), data)
            last = drop[-1]
            at = last.last - state.through
            head = bytes(state.hashes[(at - 1) * 32 : at * 32]).hex()
            atomic(
                self.dir / BASE,
                self.control.sign(
                    {
                        "through_sequence": last.last,
                        "through_segment": last.number,
                        "head": head,
                        "consumed": {
                            "name": "consumed-" + digest,
                            "sha256": digest,
                            "count": len(identities),
                        },
                    }
                ).encode(),
            )
            for seg in drop:
                self._path(seg.number).unlink(missing_ok=True)
            for name in os.listdir(self.dir):
                if CONSUMED.match(name) and name != "consumed-" + digest:
                    (self.dir / name).unlink(missing_ok=True)
            sync_directory(self.dir)
            self.cache.state = None
            return len(drop)


def oldest_coverage(control: "Control", directory: Path) -> int:
    """The operations position of the oldest retained backup in `directory`; 0 when unknown.

    Only backups whose signed manifest verifies for this store count: one that does not
    cannot be restored, so it needs no operation history. A backup taken before the
    operations log existed has no position and counts as 0, which keeps everything.
    """
    positions = []
    if directory.is_dir():
        for path in directory.iterdir():
            manifest = path / "manifest"
            if not path.is_dir() or not manifest.is_file():
                continue
            try:
                body = control.verify_document(manifest)
            except (LocalError, OSError):
                continue
            if body.get("store") != control.store:
                continue
            value = body.get("operations_sequence", 0)
            positions.append(value if isinstance(value, int) and value >= 0 else 0)
    return min(positions) if positions else 0
