"""Native PostgreSQL statement adapter for the reviewed relational service queries.

Only the small, enumerated SQLite expression differences are supported. Transactions,
RLS, FTS and row locks are PostgreSQL operations; no SQLite database is involved.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from typing import Any, Callable

from memoryking.domain.local import LocalError

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


def parameters(statement: str) -> str:
    """Convert placeholders outside SQL strings; caller data is always separately bound."""
    result: list[str] = []
    quoted = False
    i = 0
    while i < len(statement):
        c = statement[i]
        if c == "'":
            result.append(c)
            if quoted and i + 1 < len(statement) and statement[i + 1] == "'":
                result.append("'")
                i += 2
                continue
            quoted = not quoted
        elif c == "?" and not quoted:
            result.append("%s")
        elif c == "%":
            result.append("%%")
        else:
            result.append(c)
        i += 1
    if quoted:
        raise LocalError("unsupported_database_statement")
    return "".join(result)


def native_statement(statement: str) -> str:
    if re.search(r"\b(PRAGMA|ATTACH|DETACH|BEGIN|COMMIT|ROLLBACK)\b", statement, re.I):
        raise LocalError("unsupported_database_statement")
    statement = statement.replace(
        # Retrieval sends FTS5 syntax: quoted terms joined by OR. websearch_to_tsquery
        # keeps those semantics (any term matches); phraseto_tsquery would demand the phrase.
        "lexical MATCH ?",
        "to_tsvector('simple',text) @@ websearch_to_tsquery('simple',?)",
    ).replace("length(CAST(body AS BLOB))", "octet_length(body)")
    if statement.startswith("INSERT OR REPLACE INTO "):
        match = re.fullmatch(r"INSERT OR REPLACE INTO (\w+) VALUES (\([^;]+\))", statement)
        if match is None or match[1] not in UPSERTS:
            raise LocalError("unsupported_database_statement")
        columns, keys = UPSERTS[match[1]]
        assignments = ",".join(f"{c}=EXCLUDED.{c}" for c in columns if c not in keys)
        statement = (
            f"INSERT INTO {match[1]} ({','.join(columns)}) VALUES {match[2]} "
            f"ON CONFLICT ({','.join(keys)}) DO UPDATE SET {assignments}"
        )
    elif statement.startswith("INSERT OR IGNORE INTO "):
        statement = statement.replace("INSERT OR IGNORE INTO ", "INSERT INTO ", 1)
        statement += " ON CONFLICT DO NOTHING"
    if " BLOB" in statement or " OR REPLACE " in statement or " OR IGNORE " in statement:
        raise LocalError("unsupported_database_statement")
    return parameters(statement)


class Record:
    """Row values support the existing explicit name/index access, without driver leakage."""

    def __init__(self, names: Sequence[str], values: Sequence[Any]):
        self.names, self.values = tuple(names), tuple(values)
        self.by_name = dict(zip(self.names, self.values, strict=True))

    def keys(self) -> tuple[str, ...]:
        return self.names

    def __getitem__(self, key: int | slice | str) -> Any:
        return self.values[key] if isinstance(key, (int, slice)) else self.by_name[key]

    def __iter__(self) -> Iterator[Any]:
        return iter(self.values)

    def __len__(self) -> int:
        return len(self.values)


def record_factory(cursor: Any) -> Any:
    names = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: Record(names, values)


class PostgresSQL:
    """A request-scoped facade; pool/transaction ownership lives in PostgresStore."""

    def __init__(self, connection: Any, active: Callable[[], bool] = lambda: True):
        self.connection = connection
        self.active = active

    def execute(self, statement: str, parameters: Sequence[Any] = ()) -> Any:
        if not self.active():
            raise LocalError("database_transaction_required")
        return self.connection.execute(native_statement(statement), parameters)

    def executemany(self, statement: str, rows: Sequence[Sequence[Any]]) -> Any:
        if not self.active():
            raise LocalError("database_transaction_required")
        cursor = self.connection.cursor()
        cursor.executemany(native_statement(statement), rows)
        return cursor

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()
