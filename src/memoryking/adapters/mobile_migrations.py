"""Join the private viewer/report schema lineage with the released SQLite lineage.

Private versions 9 and 10 mean viewer and report tables, whereas released versions
9 and 10 mean indexes and egress audit. Never infer that lineage from a number alone.
The join is atomic; a failed attempt keeps its original version and all original data.
"""

from __future__ import annotations

import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

from memoryking.domain.local import LocalError

SCHEMA_VERSION = 14
MOBILE_SCHEMA_VERSION = "1"
EXTENSIONS = {
    "egress": ("egress_audit",),
    "summary": ("session_summaries",),
    "trust": ("source_trust", "observation_holds"),
    "anchor": ("anchors",),
    "viewer": ("viewer_bindings", "viewer_grants", "viewer_admin_retries"),
    "report": (
        "report_sources",
        "report_policies",
        "report_control_retries",
        "report_uploads",
        "report_revisions",
        "report_files",
        "report_retries",
        "report_deliveries",
    ),
}


def _script(name: str) -> str:
    return Path(__file__).with_name(f"{name}_schema.sql").read_text()


def _shape(db: sqlite3.Connection, table: str) -> tuple[Any, ...]:
    # Names come exclusively from EXTENSIONS, never from a caller.
    return (
        tuple(tuple(row)[1:] for row in db.execute(f"PRAGMA table_info({table})")),
        tuple(tuple(row) for row in db.execute(f"PRAGMA foreign_key_list({table})")),
    )


@lru_cache(maxsize=1)
def _expected_shapes() -> dict[str, tuple[Any, ...]]:
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE TABLE tenants(tenant TEXT PRIMARY KEY)")
        for name in EXTENSIONS:
            db.executescript(_script(name))
        return {table: _shape(db, table) for tables in EXTENSIONS.values() for table in tables}
    finally:
        db.close()


def _execute_migration(db: sqlite3.Connection, name: str) -> None:
    # Keep all scripts in ONE caller-owned transaction, including user_version.
    script = re.sub(
        r"^(?:BEGIN IMMEDIATE;|COMMIT;|PRAGMA user_version=\d+;)\s*$",
        "",
        _script(name),
        flags=re.MULTILINE,
    )
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ""
    if statement.strip():
        raise LocalError("schema_unsupported")


def ensure_mobile_schema(db: sqlite3.Connection) -> None:
    """Accept native 8..13, private 9/10, or the complete joined version 14."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if db.in_transaction or version not in range(8, SCHEMA_VERSION + 1):
        raise LocalError("schema_unsupported")
    db.execute("BEGIN IMMEDIATE")
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        present: dict[str, bool] = {}
        expected = _expected_shapes()
        for name, group in EXTENSIONS.items():
            count = len(set(group) & tables)
            if count not in (0, len(group)):
                raise LocalError("schema_unsupported")
            present[name] = bool(count)
            for table in group:
                if table in tables and _shape(db, table) != expected[table]:
                    raise LocalError("schema_unsupported")
        marker = db.execute("SELECT value FROM meta WHERE key='mobile_schema'").fetchone()
        if version == SCHEMA_VERSION:
            if not all(present.values()) or marker is None or marker[0] != MOBILE_SCHEMA_VERSION:
                raise LocalError("schema_unsupported")
        else:
            if marker is not None:
                raise LocalError("schema_unsupported")
            private = present["viewer"]
            if private:
                if version not in (9, 10) or present["report"] != (version == 10):
                    raise LocalError("schema_unsupported")
                if any(present[name] for name in ("egress", "summary", "trust", "anchor")):
                    raise LocalError("schema_unsupported")
            else:
                if present["report"]:
                    raise LocalError("schema_unsupported")
                for name, introduced in (
                    ("egress", 10),
                    ("summary", 11),
                    ("trust", 12),
                    ("anchor", 13),
                ):
                    if present[name] != (version >= introduced):
                        raise LocalError("schema_unsupported")
            _execute_migration(db, "index")
            for name in EXTENSIONS:
                if not present[name]:
                    _execute_migration(db, name)
            db.execute("INSERT INTO meta VALUES ('mobile_schema', ?)", (MOBILE_SCHEMA_VERSION,))
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        db.commit()
    except BaseException:
        db.rollback()
        raise
