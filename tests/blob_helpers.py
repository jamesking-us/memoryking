"""An in-memory blob store: the second implementation of the blob-store port, used only in tests.

It exists to prove the port is enough: the store core, recall, purge, backup and restore run on it
without any file for a blob. It is not a product feature and holds nothing across processes."""

import hashlib
import os
import threading
from pathlib import Path
from typing import BinaryIO

from memoryking.domain.local import LocalError


class MemoryBlobStore:
    def __init__(self) -> None:
        self.blobs: dict[tuple[str, str], bytes] = {}
        self.guard = threading.Lock()
        self.syncs = 0

    def put(self, tenant: str, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        with self.guard:
            held = self.blobs.get((tenant, digest))
            if held is None:
                self.blobs[(tenant, digest)] = bytes(content)
            elif hashlib.sha256(held).hexdigest() != digest:
                raise LocalError("blob_corrupt")
        return digest

    def put_stream(self, tenant: str, digest: str, stream: BinaryIO) -> None:
        content = stream.read()
        if hashlib.sha256(content).hexdigest() != digest:
            raise LocalError("blob_corrupt")
        with self.guard:
            self.blobs[(tenant, digest)] = content

    def get(self, tenant: str, digest: str) -> bytes:
        with self.guard:
            content = self.blobs.get((tenant, digest))
        if content is None:
            raise FileNotFoundError(digest)
        if hashlib.sha256(content).hexdigest() != digest:
            raise LocalError("blob_corrupt")
        return content

    def head(self, tenant: str, digest: str, size: int) -> bytes:
        with self.guard:
            content = self.blobs.get((tenant, digest))
        if content is None:
            raise FileNotFoundError(digest)
        return content[:size]

    def delete(self, tenant: str, digest: str) -> int:
        with self.guard:
            return 1 if self.blobs.pop((tenant, digest), None) is not None else 0

    def digests(self, tenant: str) -> list[str]:
        with self.guard:
            return [d for t, d in self.blobs if t == tenant]

    def tenants(self) -> list[str]:
        with self.guard:
            return sorted({t for t, _ in self.blobs})

    def copy_to(self, tenant: str, digest: str, destination: Path) -> int:
        content = self.get(tenant, digest)
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(content)
        return len(content)

    def sync(self) -> None:
        self.syncs += 1

    def maintain(self, budget_s: float = 0.2, tenant: str | None = None) -> bool:
        return True
