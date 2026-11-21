"""Bounded primary-only PostgreSQL pool with explicit session sanitation."""

from __future__ import annotations

import time
from typing import Any

from psycopg.pq import TransactionStatus
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout, TooManyRequests

from memoryking.adapters.postgres_sql import record_factory
from memoryking.domain.local import LocalError


def acquire(pool: Any, timeout: float | None = None) -> Any:
    """Admission fails before a connection, transaction or mutation exists.

    A request deadline can only shorten the wait for a free connection; the pool's
    own limit is the most any caller waits."""
    limit = getattr(pool, "timeout", None)
    if timeout is not None:
        limit = timeout if limit is None else min(limit, timeout)
    try:
        return pool.getconn() if limit is None else pool.getconn(timeout=max(0.0, limit))
    except (PoolTimeout, TooManyRequests):
        raise LocalError("service_busy") from None
    except PoolClosed:
        raise LocalError("service_unavailable") from None


# Lock not available, statement timeout or cancel, deadlock, serialization failure: waiting
# resolves each, and nothing in the store retries them, so a request that meets one is busy.
BUSY_SQLSTATES = frozenset({"55P03", "57014", "40P01", "40001"})


def busy_sqlstate(exc: BaseException | None) -> str | None:
    """The SQLSTATE of a driver error that means 'try again shortly', if exc is or wraps one."""
    for _ in range(4):
        if exc is None:
            return None
        state = getattr(exc, "sqlstate", None)
        if state in BUSY_SQLSTATES:
            return str(state)
        exc = exc.__cause__ or exc.__context__
    return None


def reset(connection: Any) -> None:
    if connection.info.transaction_status != TransactionStatus.IDLE:
        connection.rollback()
    # One round trip. RESET ALL also returns the session role and authorization to what the
    # connection logged in as, which is why a checked connection stays checked (see `check`).
    connection.execute("RESET ALL; UNLISTEN *; SELECT pg_advisory_unlock_all()")
    if connection.info.transaction_status != TransactionStatus.IDLE:
        raise LocalError("pool_reset_failed")


# A connection's role and the server's durability settings change only by an administrator's
# action, never by a request (every return runs `reset`), so a request checks them when the
# connection is first used and again after this many seconds, not every time.
CHECK_SECONDS = 5.0


def checked(connection: Any, *, max_age: float = CHECK_SECONDS) -> bool:
    """Check the connection unless it passed `check` within `max_age` seconds; whether it ran."""
    when = getattr(connection, "memoryking_checked", None)
    if when is not None and time.monotonic() - when < max_age:
        return False
    check(connection)
    return True


def check(connection: Any) -> None:
    row = connection.execute(
        "SELECT pg_is_in_recovery(),rolsuper,rolbypassrls,rolcreaterole,rolcreatedb,rolreplication "
        "FROM pg_roles WHERE rolname=current_user"
    ).fetchone()
    if row is None or any(row):
        raise LocalError("primary_nonprivileged_runtime_required")
    owner = connection.execute(
        "SELECT 1 FROM pg_tables WHERE schemaname='memoryking' AND tableowner=current_user LIMIT 1"
    ).fetchone()
    if owner:
        raise LocalError("runtime_table_owner_forbidden")
    for setting in ("fsync", "synchronous_commit", "full_page_writes"):
        if connection.execute(f"SHOW {setting}").fetchone()[0] != "on":
            raise LocalError("database_durability_required")
    connection.memoryking_checked = time.monotonic()


def create_pool(dsn: str, *, size: int = 8, timeout: float = 5) -> Any:
    if not 1 <= size <= 64 or not 0 < timeout <= 30:
        raise LocalError("invalid_pool_configuration")
    pool = ConnectionPool(
        dsn,
        min_size=1,
        max_size=size,
        max_waiting=64,
        timeout=timeout,
        kwargs={
            "autocommit": True,
            "row_factory": record_factory,
            "prepare_threshold": None,
            "application_name": "memoryking",
        },
        reset=reset,
        check=check,
        open=False,
    )
    pool.open(wait=True, timeout=timeout)
    return pool
