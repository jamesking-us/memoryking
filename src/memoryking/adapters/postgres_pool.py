"""Bounded primary-only PostgreSQL pool with explicit session sanitation."""

from __future__ import annotations

from typing import Any

from psycopg.pq import TransactionStatus
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout, TooManyRequests

from memoryking.adapters.postgres_sql import record_factory
from memoryking.domain.local import LocalError


def acquire(pool: Any) -> Any:
    """Admission fails before a connection, transaction or mutation exists."""
    try:
        return pool.getconn()
    except (PoolTimeout, TooManyRequests):
        raise LocalError("service_busy") from None
    except PoolClosed:
        raise LocalError("service_unavailable") from None


def reset(connection: Any) -> None:
    if connection.info.transaction_status != TransactionStatus.IDLE:
        connection.rollback()
    connection.execute("RESET ALL")
    connection.execute("UNLISTEN *")
    connection.execute("SELECT pg_advisory_unlock_all()")
    if connection.info.transaction_status != TransactionStatus.IDLE:
        raise LocalError("pool_reset_failed")


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
