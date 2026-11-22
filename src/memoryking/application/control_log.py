"""The control-log port: the signed record of policy, its anchor and the operations log.

Authority over what the store may do does not live in the database: grants, purges, policy
changes and the operations of working state are appended to a signed, hash-chained journal
that the database is checked against, and an anchor records how far that journal and the
store's history have got, so a restored, rolled-back or truncated store is refused instead of
trusted. The store core needs the operations below and nothing else; the files, the signing key
on disk, the byte-level verification cache and the segment compaction are details of the
implementation (`adapters/local_files.py`, `adapters/operations_log.py`).

What an implementation must guarantee, because the store's security rests on it:

* Every record and the anchor are signed with a key the database cannot reach; `verify` and
  `read_anchor` raise `LocalError("invalid_token")` / `LocalError("recovery_required")` for
  anything not signed by that key, and comparison is constant time.
* The journal is hash-chained: a record names the hash of its predecessor, `append` refuses to
  write when the log is not exactly what the anchor describes, and `records_after` / `journal`
  / `verified_count` raise instead of returning records from a log that was truncated,
  rolled back, reordered or replaced (an unacknowledged torn final line is the one tolerated
  damage).
* `append` is durable before it returns, and it moves the anchor's sequence and head to the
  record it wrote.
* The operations log is a second chain of its own, with its own anchor fields, that can be
  compacted only up to what a retained backup already holds.

There is no in-memory implementation: a test double for this port would have to reimplement
the signing, chaining and anchoring above, which is exactly what the journal, anchor, restore
and fault-injection tests exist to hold the one implementation to.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol


class OperationsLogState(Protocol):
    """What a verified read of the operations log reports."""

    count: int
    head: str


class OperationsLog(Protocol):
    """The operations log: records of working-state operations, chained apart from the journal."""

    def append(self, anchor: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]: ...

    def records_after(self, anchor: dict[str, Any], sequence: int) -> list[dict[str, Any]]: ...

    def verified_count(self, anchor: dict[str, Any]) -> int: ...

    def head(self, count: int) -> str: ...

    def sync(self, anchor: dict[str, Any], *, trust_anchor: bool = False) -> OperationsLogState: ...

    def compacted_through(self) -> int: ...

    def consumed(self, tenant: str, identity: str) -> bool: ...

    def compact(
        self,
        anchor: dict[str, Any],
        coverage: int,
        resolved: Callable[[Iterable[tuple[str, str]]], bool],
    ) -> int: ...


class ControlLog(Protocol):
    """The signed journal, its anchor and the signing every credential and archive uses."""

    @property
    def store(self) -> str:
        """The identity of the store this control tree belongs to."""
        ...

    @property
    def path(self) -> Path: ...

    @property
    def operations(self) -> OperationsLog: ...

    def sign(self, body: dict[str, Any]) -> str: ...

    def verify(self, token: str) -> dict[str, Any]: ...

    def verify_document(self, path: Path) -> dict[str, Any]: ...

    def read_anchor(self) -> dict[str, Any]: ...

    def anchor(self, value: dict[str, Any]) -> None: ...

    def journal(self, anchor: dict[str, Any]) -> list[dict[str, Any]]: ...

    def append(self, anchor: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]: ...

    def records_after(self, anchor: dict[str, Any], sequence: int) -> list[dict[str, Any]]: ...

    def verified_count(self, anchor: dict[str, Any]) -> int: ...

    def head(self, count: int) -> str: ...

    def note_apply_failure(self, record: dict[str, Any], exc: BaseException) -> None: ...

    def apply_failure(self) -> dict[str, Any] | None: ...

    def clear_apply_failure(self, applied: int) -> None: ...

    def describe_pending(
        self, records: list[dict[str, Any]], applied: int, held: int
    ) -> dict[str, Any]: ...


class LockedControlLog(ControlLog, Protocol):
    """A control log several processes share: one writer at a time across them."""

    def locked(self, timeout: float = 5) -> AbstractContextManager[None]: ...
