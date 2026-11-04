"""Offline, transaction-owned join of the native and private shared schema lineages."""

from pathlib import Path
from typing import Any

from memoryking.adapters.mobile_migrations import EXTENSIONS
from memoryking.domain.local import LocalError

SCHEMA_VERSION = "9"


def ensure_mobile_schema(connection: Any) -> None:
    """Caller holds the migration role, transaction and schema advisory lock."""
    version = connection.execute("SELECT value FROM shared_meta WHERE key='schema'").fetchone()[0]
    if version not in {"3", "4", "5", "6", "7", "8", SCHEMA_VERSION}:
        raise LocalError("schema_unsupported")
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='memoryking'"
        )
    }
    present = {}
    for name, group in EXTENSIONS.items():
        count = len(set(group) & tables)
        if count not in (0, len(group)):
            raise LocalError("schema_unsupported")
        present[name] = bool(count)
    marker = connection.execute(
        "SELECT value FROM shared_meta WHERE key='mobile_schema'"
    ).fetchone()
    if version == SCHEMA_VERSION:
        if not all(present.values()) or marker is None or marker[0] != "1":
            raise LocalError("schema_unsupported")
        return
    if marker is not None:
        raise LocalError("schema_unsupported")
    private = present["viewer"]
    if private:
        if version not in {"4", "5"} or present["report"] != (version == "5"):
            raise LocalError("schema_unsupported")
        if any(present[name] for name in ("egress", "summary", "trust", "anchor")):
            raise LocalError("schema_unsupported")
    else:
        if present["report"]:
            raise LocalError("schema_unsupported")
        for name, introduced in (("egress", 5), ("summary", 6), ("trust", 7), ("anchor", 8)):
            if present[name] != (int(version) >= introduced):
                raise LocalError("schema_unsupported")
    if private or version == "3":
        for name in ("counter", "index_schema"):
            connection.execute(Path(__file__).with_name(f"postgres_{name}.sql").read_text())
    for name in EXTENSIONS:
        if not present[name]:
            connection.execute(Path(__file__).with_name(f"postgres_{name}_schema.sql").read_text())
    connection.execute("INSERT INTO shared_meta VALUES ('mobile_schema','1')")
    connection.execute("UPDATE shared_meta SET value=%s WHERE key='schema'", (SCHEMA_VERSION,))
