"""Short process-safe locks for the single-host independent tenant control volume."""

from __future__ import annotations

import fcntl
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from memoryking.adapters.local_files import Control, private_file
from memoryking.domain.local import LocalError


class SharedControl(Control):
    def __init__(self, path: Path):
        super().__init__(path)
        self.thread_lock = threading.RLock()

    @contextmanager
    def locked(self, timeout: float = 5) -> Iterator[None]:
        with self.thread_lock:
            path = self.path / "control.lock"
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                private_file(path)
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise LocalError("control_busy") from None
                        time.sleep(0.01)
                yield
            finally:
                os.close(fd)
