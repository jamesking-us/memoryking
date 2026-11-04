"""The storage port's dialect: the few expressions SQLite and PostgreSQL spell differently.

Everything else the store and its adapters write is portable SQL: `?` placeholders
(the PostgreSQL facade binds them), `ON CONFLICT` upserts, and the JSON table function
`json_each`, which the PostgreSQL schema defines. A backend hands its dialect to the
store, and no adapter may emit engine-specific syntax: the PostgreSQL facade refuses it
rather than translating it, so a new divergence is a test failure, not a regex.
"""

from __future__ import annotations

from typing import Protocol

# Upsert targets: the full column list and the conflict key for each table that is
# written with "insert or replace" semantics. Both engines accept the generated SQL.
UPSERTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "grants": (
        ("tenant", "principal", "project", "capabilities", "starts", "expires"),
        ("tenant", "principal", "project"),
    ),
    "projection_items": (
        (
            "tenant",
            "observation",
            "incarnation",
            "generation",
            "source_revision",
            "blob_hash",
            "byte_count",
        ),
        ("tenant", "observation"),
    ),
    "code_policies": (
        (
            "tenant",
            "revision",
            "repository",
            "origin",
            "version",
            "impact",
            "dependencies",
            "evidence",
            "actor",
        ),
        ("tenant", "revision"),
    ),
    "source_trust": (
        ("tenant", "project", "source", "level", "decided_by", "decided"),
        ("tenant", "project", "source"),
    ),
    "code_validations": (
        (
            "tenant",
            "incarnation",
            "revision",
            "snapshot",
            "policy",
            "state",
            "evidence",
            "expires",
            "reason",
            "generation",
        ),
        ("tenant", "incarnation", "revision", "snapshot", "policy"),
    ),
}


def upsert(table: str) -> str:
    """INSERT ... ON CONFLICT (keys) DO UPDATE for a registered table; portable SQL."""
    columns, keys = UPSERTS[table]
    assignments = ",".join(f"{c}=EXCLUDED.{c}" for c in columns if c not in keys)
    placeholders = ",".join("?" for _ in columns)
    return (
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({','.join(keys)}) DO UPDATE SET {assignments}"
    )


def insert_ignore(table: str, count: int) -> str:
    """INSERT ... ON CONFLICT DO NOTHING; portable SQL."""
    placeholders = ",".join("?" for _ in range(count))
    return f"INSERT INTO {table} VALUES ({placeholders}) ON CONFLICT DO NOTHING"


class Dialect(Protocol):
    name: str

    def lexical_match(self) -> str:
        """A predicate over the `lexical` table with one `?` bound to the query string."""
        ...

    def byte_length(self, expression: str) -> str:
        """The byte length of a text column."""
        ...

    def report_boundary(self, field: str) -> str:
        """Integer starts/expires field of a report source's JSON body."""
        ...


class SQLiteDialect:
    name = "sqlite"

    def lexical_match(self) -> str:
        return "lexical MATCH ?"

    def byte_length(self, expression: str) -> str:
        return f"length(CAST({expression} AS BLOB))"

    def report_boundary(self, field: str) -> str:
        if field not in {"starts", "expires"}:
            raise ValueError("unknown report boundary")
        return f"CAST(json_extract(body,'$.{field}') AS BIGINT)"


class PostgresDialect:
    name = "postgresql"

    def lexical_match(self) -> str:
        # Retrieval sends quoted terms joined by OR; websearch_to_tsquery keeps that
        # meaning (any term matches) where phraseto_tsquery would demand the phrase.
        return "to_tsvector('simple',text) @@ websearch_to_tsquery('simple',?)"

    def byte_length(self, expression: str) -> str:
        return f"octet_length({expression})"

    def report_boundary(self, field: str) -> str:
        if field not in {"starts", "expires"}:
            raise ValueError("unknown report boundary")
        return f"CAST(body::jsonb->>'{field}' AS BIGINT)"
