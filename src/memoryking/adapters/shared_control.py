"""Short process-safe locks for the single-host independent tenant control volume."""

from __future__ import annotations

import fcntl
import hashlib
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from memoryking.adapters.local_files import Control, JournalCache, private_file
from memoryking.adapters.operations_log import OperationsLog
from memoryking.domain.local import LocalError

# A store, and so a `SharedControl`, is built per request. The verified journal prefix is kept
# per control tree and signing key for the life of the process so a request verifies only
# what was appended since the last one; see `Control._usable` for when it is trusted.
_CACHES: dict[tuple[str, str], JournalCache] = {}
_CACHES_LOCK = threading.Lock()
_CACHE_LIMIT = 512


def _journal_cache(path: Path, key: bytes) -> JournalCache:
    identity = (os.path.realpath(path), hashlib.sha256(b"journal-cache" + key).hexdigest())
    with _CACHES_LOCK:
        cache = _CACHES.pop(identity, None)
        if cache is None:
            cache = JournalCache(keep_records=False)
        _CACHES[identity] = cache
        while len(_CACHES) > _CACHE_LIMIT:
            _CACHES.pop(next(iter(_CACHES)))
        return cache


class SharedControl(Control):
    def __init__(self, path: Path):
        super().__init__(path)
        self._cache = _journal_cache(path, self.key)
        self.operations = OperationsLog(self, self._cache.operations)
        self.thread_lock = threading.RLock()

    @contextmanager
    def locked(self, timeout: float = 5) -> Iterator[None]:
        with self.thread_lock:
            path = self.path / "control.lock"
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                private_file(path)
                deadline = time.monotonic() + timeout
                pause = 0.0005  # the lock is held for one small write: poll fast, then back off
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise LocalError("control_busy") from None
                        time.sleep(pause)
                        pause = min(pause * 2, 0.01)
                yield
            finally:
                os.close(fd)
