"""The blob-store port: where observation bodies live.

An observation's text is a blob named by the SHA-256 of its bytes. The store core writes it
once, reads it back verified against that name, deletes it when no observation names it any
more, and lists what a tenant holds so a sweep can find orphans. A backup copies blobs out and a
restore puts them back. Those are the only things the core asks of a blob store, and they are
all here; the directory layout, hard-linked backups and the migration from the flat layout are
details of the file implementation (`adapters/blobs.py`), not of the port.

What an implementation must guarantee:

* `put` returns only after the bytes are durable, and is idempotent: putting content that is
  already held changes nothing. If the held bytes do not hash to their name it raises
  `LocalError("blob_corrupt")` rather than trust them.
* `get` returns bytes that hash to `digest` or raises `LocalError("blob_corrupt")`. A blob that
  is not held raises `FileNotFoundError` (any `OSError` is treated by callers as unreadable).
* Blobs are immutable and a tenant's blobs are separate from every other tenant's: the same
  digest under two tenants is two blobs.
* `delete` and `put_stream` take effect at once and become durable at the next `sync`.
* `digests` and `tenants` list what is held, so the store can sweep what nothing names.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO, Protocol


class BlobStore(Protocol):
    def put(self, tenant: str, content: bytes) -> str:
        """Store the content durably; returns its SHA-256 hex digest."""
        ...

    def put_stream(self, tenant: str, digest: str, stream: BinaryIO) -> None:
        """Store a blob read from a stream (a restore); the bytes must hash to `digest`
        or `LocalError("blob_corrupt")` is raised and nothing is kept. Durable at `sync`."""
        ...

    def get(self, tenant: str, digest: str) -> bytes:
        """The verified bytes of a blob."""
        ...

    def head(self, tenant: str, digest: str, size: int) -> bytes:
        """At most `size` leading bytes, not verified (for bounded reads)."""
        ...

    def delete(self, tenant: str, digest: str) -> int:
        """Remove a blob; 1 if it was held, else 0. Durable at `sync`."""
        ...

    def digests(self, tenant: str) -> list[str]:
        """Every digest held for the tenant."""
        ...

    def tenants(self) -> Iterable[str]:
        """Every tenant that holds blobs."""
        ...

    def copy_to(self, tenant: str, digest: str, destination: Path) -> int:
        """Write a verified copy of the blob to a local file (a backup); returns its size.
        Raises `FileNotFoundError` when the blob is not held."""
        ...

    def sync(self) -> None:
        """Make every earlier `delete` and `put_stream` durable."""
        ...

    def maintain(self, budget_s: float = 0.2, tenant: str | None = None) -> bool:
        """Bounded housekeeping (the file store moves flat blobs into shard directories);
        True when there is nothing left to do."""
        ...
