"""One deadline for canonical viewer work, including rollback-safe cancellation."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from memoryking.application.store_core import StoreCore
from memoryking.domain.local import LocalError


@contextmanager
def request_budget(store: StoreCore, deadline: float | None) -> Iterator[None]:
    if deadline is None:
        yield
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not store.lock.acquire(timeout=remaining):
        raise LocalError("service_unavailable")
    timer = None
    interrupted = False

    def progress() -> int:
        nonlocal interrupted
        if not interrupted and time.monotonic() >= deadline:
            interrupted = True
            return 1
        return 0

    try:
        if isinstance(store.db, sqlite3.Connection):
            store.db.set_progress_handler(progress, 1000)
        elif hasattr(store, "connection"):

            def cancel() -> None:
                try:
                    store.connection.cancel_safe(timeout=1)
                except Exception:
                    pass  # The request still owns the connection until cancellation is joined.

            timer = threading.Timer(max(0, deadline - time.monotonic()), cancel)
            timer.daemon = True
            timer.start()
        yield
    except Exception as exc:
        if time.monotonic() >= deadline:
            raise LocalError("service_unavailable") from exc
        raise
    finally:
        if timer is not None:
            timer.cancel()
            timer.join()
        if isinstance(store.db, sqlite3.Connection):
            store.db.set_progress_handler(None, 0)
        store.lock.release()
