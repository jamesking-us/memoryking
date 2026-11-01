"""Native PostgreSQL statement facade for the store's portable SQL.

The store and its adapters write engine-neutral SQL and ask the dialect for the few
expressions that differ, so this facade only binds placeholders. Anything SQLite-only
that slips through is refused here, which makes a new divergence a test failure.
Transactions, RLS, FTS and row locks are PostgreSQL operations; no SQLite is involved.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from typing import Any, Callable

from memoryking.domain.local import LocalError


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
    """Portable SQL with PostgreSQL placeholders; engine-specific syntax is refused, not translated."""
    # Keywords are written in upper case; lower-case column names such as `blob` pass.
    if re.search(
        r"\b(PRAGMA|ATTACH|DETACH|BEGIN|COMMIT|ROLLBACK|MATCH|BLOB|GLOB)\b|INSERT OR ", statement
    ):
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
