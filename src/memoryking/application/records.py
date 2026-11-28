"""The record-type registry: one declaration of what each table the store owns is.

Several parts of the store need a list of tables with one property each: which are archived
in a shared backup and from which archive schema, which a purge of an observation reaches,
which a restore must reset because it starts a new history, which bounds are counted, which
a worker may write. Those lists used to be written out where they were used and every
migration had to remember each of them. They are derived from the declarations below, so a
new table is added once, here, and a test refuses a schema that has a table this module does
not name (tests/test_records.py runs it on both backends).

Where a property needs real logic (the dependency walk of a purge, retention windows, the
capacity counters) the logic stays where it was; the registry names the types it covers and
a test asserts that the two agree.

This module is part of the application layer: it names tables and holds portable SQL only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# The newest archive schema a shared backup is written with. A table joins the archive in the
# schema named by `archived_in`; older archives stay restorable with the tables they held.
ARCHIVE_SCHEMA = 14

Backend = Literal["both", "shared", "local", "pgvector"]
Scope = Literal["tenant", "none"]
Dependency = Literal["checkpoint", "experience_record", "artifact"]
Purge = Literal["tombstone", "delete", "noop", "clear_result", "retire", "write"]
Retention = Literal["window", "expire", "frontier", "orphan"]
NewHistory = Literal["delete", "clear_result", "retire", "reset_ranking", "reset_partition"]
Kind = Literal[
    "evidence",
    "policy",
    "knowledge",
    "working",
    "derived",
    "audit",
    "bookkeeping",
    "index",
]


@dataclass(frozen=True)
class RecordType:
    """What the store's lists need to know about one table (or view)."""

    table: str
    kind: Kind
    # Rows carry a tenant column and, on the shared backend, a forced row-level-security policy.
    scope: Scope = "tenant"
    # Which backends have the table at all (the search-engine internals of SQLite and the
    # converted vector copy of an optional PostgreSQL extension are not the store's records).
    backends: Backend = "both"
    # The archive schema in which a shared backup first holds the table; None when a backup
    # does not copy it (it is rebuilt, or it belongs to one process family).
    archived_in: int | None = None
    # Rows hold a list of the observations and claims they were built from, and a purge
    # follows that list (store_core._purge_dependents).
    dependency: Dependency | None = None
    # How purging an observation changes this table.
    purge: Purge | None = None
    # How a bound on its size is kept (application/retention.py and expire_results).
    retention: Retention | None = None
    # The capacity counters (CAPACITY_LIMITS) that count its rows or bytes.
    capacity: tuple[str, ...] = ()
    # What a restore does to it, because a restore creates a new history.
    new_history: NewHistory | None = None
    # A background worker may write it (the shared worker role's grants).
    worker_writes: bool = False
    # Rows belong to an embedding partition and go with it when the partition is rebuilt.
    partition: bool = False


def _t(table: str, kind: Kind, **properties: Any) -> RecordType:
    return RecordType(table, kind, **properties)


# Archive order: the order shared backups write the tables, and a restore reads them. A restore
# deletes in reverse, so a table never precedes one it refers to.
RECORD_TYPES: tuple[RecordType, ...] = (
    _t("tenants", "bookkeeping", archived_in=1, purge="write"),
    _t("grants", "policy", archived_in=1),
    _t("events", "evidence", archived_in=1, purge="write"),
    _t("observations", "evidence", archived_in=1, purge="tombstone"),
    _t("lexical", "index", archived_in=1, purge="delete"),
    _t(
        "retries",
        "bookkeeping",
        archived_in=1,
        purge="clear_result",
        retention="expire",
        new_history="clear_result",
    ),
    _t(
        "jobs",
        "bookkeeping",
        archived_in=1,
        purge="noop",
        retention="frontier",
        capacity=("jobs",),
        worker_writes=True,
    ),
    _t("projections", "derived", archived_in=1, worker_writes=True),
    _t("projection_items", "derived", archived_in=1, purge="delete", worker_writes=True),
    _t(
        "receipts", "audit", archived_in=1, purge="delete", retention="expire", new_history="delete"
    ),
    _t("retired_sources", "policy", archived_in=1, purge="write"),
    _t("knowledge_state", "knowledge", archived_in=1, purge="write"),
    _t("families", "knowledge", archived_in=1),
    _t("revisions", "knowledge", archived_in=1),
    _t("evidence_spans", "knowledge", archived_in=1),
    _t("lifecycle", "knowledge", archived_in=1),
    _t("replacements", "knowledge", archived_in=1),
    _t("conflicts", "knowledge", archived_in=1),
    _t("attestations", "knowledge", archived_in=1),
    _t(
        "rendered_artifacts",
        "derived",
        archived_in=1,
        dependency="artifact",
        purge="retire",
        retention="expire",
        capacity=("rendered_artifacts", "rendered_bytes"),
        new_history="retire",
    ),
    _t("checkpoint_streams", "working", archived_in=1),
    _t(
        "checkpoints",
        "working",
        archived_in=1,
        dependency="checkpoint",
        purge="retire",
        retention="window",
        capacity=("checkpoints", "checkpoint_bytes"),
        new_history="retire",
    ),
    _t("operation_intents", "working", archived_in=1, capacity=("operation_intents",)),
    _t(
        "handoffs",
        "working",
        archived_in=1,
        purge="retire",
        capacity=("handoffs",),
        new_history="retire",
    ),
    _t(
        "handoff_views",
        "working",
        archived_in=1,
        retention="orphan",
        capacity=("handoff_views",),
    ),
    _t("repositories", "working", archived_in=1),
    _t("repository_clones", "working", archived_in=1),
    _t("repository_snapshots", "working", archived_in=1),
    _t("code_policies", "policy", archived_in=1),
    _t("code_validations", "working", archived_in=1, worker_writes=True),
    _t("repository_jobs", "bookkeeping", archived_in=1, worker_writes=True),
    _t("tenant_gates", "bookkeeping", backends="shared", archived_in=1),
    _t("tenant_meta", "bookkeeping", backends="shared", archived_in=1),
    _t(
        "embedding_partitions",
        "derived",
        archived_in=2,
        new_history="reset_partition",
    ),
    _t(
        "embedding_jobs",
        "bookkeeping",
        archived_in=2,
        purge="noop",
        new_history="delete",
        worker_writes=True,
        partition=True,
    ),
    _t(
        "embedding_items",
        "derived",
        archived_in=2,
        purge="delete",
        new_history="delete",
        worker_writes=True,
        partition=True,
    ),
    _t(
        "processing_usage",
        "audit",
        archived_in=2,
        retention="expire",
        capacity=("processing_usage",),
        worker_writes=True,
    ),
    _t("adapter_bases", "derived", archived_in=2, purge="delete", new_history="delete"),
    _t(
        "experience_records",
        "working",
        archived_in=3,
        dependency="experience_record",
        purge="retire",
        retention="window",
        capacity=("experience_records",),
    ),
    _t("ranking_state", "policy", archived_in=3, purge="write", new_history="reset_ranking"),
    _t("ranking_history", "policy", archived_in=3),
    _t("egress_audit", "audit", archived_in=4, retention="expire"),
    _t("session_summaries", "derived", archived_in=4, purge="delete", retention="expire"),
    _t("source_trust", "policy", archived_in=5),
    _t("observation_holds", "policy", archived_in=5),
    _t("anchors", "index", archived_in=6, purge="delete"),
    _t("credential_generations", "policy", archived_in=8),
    _t("lexical_state", "index", archived_in=9),
    _t("lexical_projects", "index", archived_in=9, purge="write"),
    _t("lexical_terms", "index", archived_in=9, purge="write"),
    _t("supersessions", "knowledge", archived_in=10, purge="write"),
    _t("retirement_requests", "policy", archived_in=10, purge="write"),
    _t("pins", "policy", archived_in=10, purge="write"),
    _t("consolidation_runs", "audit", archived_in=12),
    _t("unreviewed_tier", "policy", archived_in=12),
    _t(
        "embedding_vectors",
        "derived",
        archived_in=13,
        purge="delete",
        new_history="delete",
        worker_writes=True,
        partition=True,
    ),
    _t(
        "embedding_skips",
        "derived",
        archived_in=13,
        new_history="delete",
        worker_writes=True,
    ),
    _t("viewer_registries", "policy", archived_in=14),
    _t("viewer_mappings", "policy", archived_in=14),
    _t("viewer_receipt_context", "audit", archived_in=14, purge="delete", new_history="delete"),
    # Lease hashes are ephemeral, never replayed or restored as authority.
    _t("viewer_leases", "bookkeeping", new_history="delete"),
    # Not copied into a shared backup.
    _t("meta", "bookkeeping", worker_writes=True),
    _t("instance_lease", "bookkeeping", new_history="delete"),
    _t("receipt_observations", "audit", purge="delete"),
    _t("shared_meta", "bookkeeping", scope="none", backends="shared"),
    _t("embedding_native", "derived", backends="pgvector"),
    # SQLite's full-text engine keeps its own shadow tables beside `lexical`.
    _t("lexical_data", "index", scope="none", backends="local"),
    _t("lexical_idx", "index", scope="none", backends="local"),
    _t("lexical_content", "index", scope="none", backends="local"),
    _t("lexical_docsize", "index", scope="none", backends="local"),
    _t("lexical_config", "index", scope="none", backends="local"),
)

BY_TABLE: dict[str, RecordType] = {record.table: record for record in RECORD_TYPES}
assert len(BY_TABLE) == len(RECORD_TYPES), "a table is declared twice"


def archive_tables(schema: int) -> tuple[str, ...]:
    """The tables a shared backup of this archive schema holds, in archive order."""
    return tuple(
        r.table for r in RECORD_TYPES if r.archived_in is not None and r.archived_in <= schema
    )


def worker_tables() -> tuple[str, ...]:
    """Tables the shared worker role may insert into, update and delete from."""
    return tuple(r.table for r in RECORD_TYPES if r.worker_writes)


def partition_tables() -> tuple[str, ...]:
    """Tables whose rows belong to one embedding partition."""
    return tuple(r.table for r in RECORD_TYPES if r.partition)


def dependency_tables() -> tuple[str, ...]:
    """Tables whose rows name what they were built from; a purge follows those names."""
    return tuple(r.table for r in RECORD_TYPES if r.dependency is not None)


def capacity_counters() -> tuple[str, ...]:
    """Every counter some table declares, in declaration order."""
    return tuple(name for r in RECORD_TYPES for name in r.capacity)


def tables_of(backend: Literal["local", "shared"]) -> frozenset[str]:
    """The declared tables a backend's schema is expected to hold."""
    wanted = {"both", backend}
    return frozenset(r.table for r in RECORD_TYPES if r.backends in wanted)


# What a restore does to a table. Each statement is portable SQL ending where a WHERE clause
# may follow; a local restore covers every tenant and a shared restore names its one tenant.
_RETIRE = {
    "rendered_artifacts": (
        "UPDATE rendered_artifacts SET state='retired',body=NULL,request_json=NULL,"
        "diagnostics=NULL,dependencies='[]'"
    ),
    "checkpoints": "UPDATE checkpoints SET state='retired',body=NULL,dependencies='[]'",
    "handoffs": "UPDATE handoffs SET state='retired'",
}
_RESET = {
    "retries": "UPDATE retries SET result=NULL",
    "ranking_state": "UPDATE ranking_state SET configuration=NULL,version=version+1",
}
_PARTITION_RESET = (
    "UPDATE embedding_partitions SET incarnation=?,state='disabled',generation=1,"
    "scheduled=0,frontier=0"
)


def retire_for_new_history(db: Any, incarnation: str, tenant: str | None = None) -> None:
    """A restored history cannot reuse replay results, receipts, retained artifacts,
    checkpoint bodies, projected vectors, leases or adapter bases: reset every type that says so.

    A local restore replaces the whole store's history, so it passes no tenant and the
    statements cover every tenant; the shared restore names its tenant in each one."""
    scope, bound = (" WHERE tenant=?", (tenant,)) if tenant else ("", ())
    for record in RECORD_TYPES:
        action, table = record.new_history, record.table
        if action is None:
            continue
        if action == "delete":
            db.execute(f"DELETE FROM {table}" + scope, bound)
        elif action == "clear_result":
            db.execute(_RESET[table] + scope, bound)
        elif action == "reset_ranking":
            db.execute(_RESET[table] + scope, bound)
        elif action == "retire":
            db.execute(_RETIRE[table] + scope, bound)
        elif action == "reset_partition":
            db.execute(_PARTITION_RESET + scope, (incarnation, *bound))
        else:  # pragma: no cover - the Literal above is closed
            raise AssertionError(action)


# Tables retire_for_new_history has a statement for; a test compares it with the registry.
NEW_HISTORY_STATEMENTS = frozenset(_RETIRE) | frozenset(_RESET) | {"embedding_partitions"}
