"""Read a detached legacy SQLite snapshot without executing its schema or code.

The snapshot is evidence, never authority. A plan must account for every ordinary
table. Virtual/shadow search tables are inventoried but rebuilt rather than copied.
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
import stat
from collections import deque
from pathlib import Path
from typing import Any

from memoryking.adapters.legacy_tables import TABLES
from memoryking.contracts.canonical import canonical_bytes, canonical_hash
from memoryking.domain.local import LocalError

MAX_DATABASE = 512 * 1024 * 1024
MAX_ROWS = 10_000
MAX_ROW = 60 * 1024
MAX_TOTAL = 64 * 1024 * 1024
SHAPES = {
    "kingforge-v1": {
        "plans": {
            "id",
            "title",
            "objective",
            "context",
            "author",
            "status",
            "created_at",
            "updated_at",
        },
        "steps": {"id", "plan_id", "seq", "description", "status", "notes", "updated_at"},
        "lessons": {
            "id",
            "plan_id",
            "title",
            "situation",
            "lesson",
            "kind",
            "tags",
            "author",
            "created_at",
        },
    },
    "standalone": {
        "documents": {
            "id",
            "kind",
            "feature",
            "title",
            "content",
            "status",
            "content_hash",
            "created_at",
            "updated_at",
        },
        "lessons": {
            "id",
            "category",
            "title",
            "body",
            "source_feature",
            "created_at",
            "updated_at",
        },
    },
    "universal": {
        "lw_schema_migrations": {"version", "name", "applied_at"},
        "lw_scopes": {"id", "parent_scope_id", "kind", "external_key", "lifecycle"},
        "lw_evidence": {"id", "scope_id", "content", "content_hash"},
        "lw_memories": {
            "id",
            "scope_id",
            "memory_key",
            "revision",
            "type",
            "title",
            "body",
            "lifecycle",
        },
    },
}


def digest_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def cell(value: Any) -> dict[str, Any]:
    """Preserve SQLite storage type; vectors remain opaque source metadata."""
    if value is None:
        return {"type": "null"}
    if isinstance(value, bytes):
        return {"type": "blob", "sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)}
    if isinstance(value, str):
        return {"type": "text", "value": value}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float) and math.isfinite(value):
        return {"type": "real", "hex": value.hex()}
    raise LocalError("legacy_value_unsupported")


def inspect(path: Path, source: str, *, expected_profiles: list[str]) -> dict[str, Any]:
    """Inspect a closed snapshot. Callers supply a stable source-system identity.

    Refuse WAL/journal sidecars instead of treating an active database as a backup.
    Only a detached copy is opened by SQLite, using immutable read-only mode.
    The caller may not use this function to take a live backup.
    """
    from memoryking.adapters.local_store import identifier

    identifier(source)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_DATABASE:
        raise LocalError("legacy_snapshot_invalid")
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise LocalError("legacy_snapshot_not_detached")
    before = digest_file(path)
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        # Bound malicious/accidentally huge queries as well as returned bytes.
        steps = 0

        def deadline() -> int:
            nonlocal steps
            steps += 1
            return int(steps > 100_000)

        db.set_progress_handler(deadline, 1000)
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise LocalError("legacy_integrity_failed")
        table_list = list(db.execute("PRAGMA table_list"))
        tables = sorted(
            r["name"]
            for r in table_list
            if r["schema"] == "main"
            and r["type"] == "table"
            and not r["name"].startswith("sqlite_")
        )
        derived = sorted(
            r["name"]
            for r in table_list
            if r["schema"] == "main" and r["type"] in {"virtual", "shadow"}
        )
        if len(tables) > 128:
            raise LocalError("legacy_table_limit")
        schema = {
            t: [dict(r) for r in db.execute("PRAGMA table_info(" + quote(t) + ")")] for t in tables
        }
        columns = {t: {c["name"] for c in schema[t]} for t in tables}
        profiles = sorted(
            p
            for p, requirements in SHAPES.items()
            if all(required <= columns.get(t, set()) for t, required in requirements.items())
        )
        unresolved: list[dict[str, Any]] = []
        if not profiles or profiles != sorted(expected_profiles):
            unresolved.append({"code": "profile_mismatch", "detected": profiles})
        rows: list[dict[str, Any]] = []
        raw_rows: dict[str, list[dict[str, Any]]] = {}
        counts: dict[str, int] = {}
        total_bytes = 0
        for table in tables:
            key = [c["name"] for c in sorted(schema[table], key=lambda c: c["pk"]) if c["pk"]]
            counts[table] = db.execute("SELECT count(*) FROM " + quote(table)).fetchone()[0]
            if sum(counts.values()) > MAX_ROWS:
                raise LocalError("legacy_row_limit")
            if table not in TABLES:
                unresolved.append({"code": "unsupported_table", "table": table})
                continue
            if not key:
                unresolved.append({"code": "missing_stable_key", "table": table})
                continue
            sql = "SELECT * FROM " + quote(table) + " ORDER BY " + ",".join(map(quote, key))
            raw_rows[table] = []
            for row in db.execute(sql):
                raw = dict(row)
                raw_rows[table].append(raw)
                identity = {k: cell(raw[k]) for k in key}
                if any(raw[k] is None for k in key):
                    unresolved.append({"code": "null_stable_key", "table": table})
                row_key = canonical_hash({"table": table, "key": identity})
                values = {k: cell(v) for k, v in raw.items()}
                body = {
                    "format": "memoryking-legacy-row-v1",
                    "source": source,
                    "table": table,
                    "key": identity,
                    "values": values,
                    "authority": "untrusted_historical_evidence",
                    "time_semantics": "original_values_only_not_current_validity",
                    "embedding_semantics": "opaque_hash_not_an_index",
                }
                encoded = canonical_bytes(body)
                total_bytes += len(encoded)
                if total_bytes > MAX_TOTAL:
                    raise LocalError("legacy_total_limit")
                blocked = len(encoded) > MAX_ROW
                if blocked:
                    unresolved.append({"code": "record_too_large", "table": table, "key": row_key})
                if table in {"documents", "lw_evidence"} and isinstance(raw.get("content"), str):
                    expected = hashlib.sha256(raw["content"].encode()).hexdigest()
                    if str(raw.get("content_hash", "")).lower() != expected:
                        unresolved.append(
                            {"code": "content_hash_mismatch", "table": table, "key": row_key}
                        )
                retired = any(
                    str(raw.get(k, "")).lower() in {"deleted", "purged", "revoked"}
                    for k in ("status", "lifecycle")
                )
                rows.append(
                    {
                        "table": table,
                        "key": row_key,
                        "identity": identity,
                        "sha256": hashlib.sha256(encoded).hexdigest(),
                        "body": body,
                        "retired": retired,
                        "references": [],
                    }
                )
        lookup = {
            (r["table"], tuple((k, canonical_bytes(v)) for k, v in r["identity"].items())): r["key"]
            for r in rows
        }
        row_map = {r["key"]: r for r in rows}
        # KingForge projections intentionally omit SQL FKs to compatibility
        # tables in the legacy schema; resolve those source links explicitly.
        projection_sources = {
            "lw_kingforge_v1_plan_projections": ("plan_id", "plans"),
            "lw_kingforge_v1_lesson_projections": ("lesson_id", "lessons"),
        }
        for table, (column, target) in projection_sources.items():
            for row, raw in zip(
                [r for r in rows if r["table"] == table], raw_rows.get(table, []), strict=True
            ):
                ref = lookup.get((target, (("id", canonical_bytes(cell(raw.get(column)))),)))
                if ref is None:
                    unresolved.append(
                        {"code": "unresolved_projection_source", "table": table, "key": row["key"]}
                    )
                else:
                    row["references"].append(ref)
        # Resolve every declared foreign key, including composite keys. Partial
        # NULL keys have SQLite's non-reference semantics and remain in raw data.
        for table, originals in raw_rows.items():
            foreign: dict[int, list[dict[str, Any]]] = {}
            for fk in db.execute("PRAGMA foreign_key_list(" + quote(table) + ")"):
                foreign.setdefault(fk["id"], []).append(dict(fk))
            by_table = [r for r in rows if r["table"] == table]
            for raw, row in zip(originals, by_table, strict=True):
                for group in foreign.values():
                    group.sort(key=lambda f: f["seq"])
                    if any(raw[f["from"]] is None for f in group):
                        continue
                    target = group[0]["table"]
                    target_pk = [
                        c["name"]
                        for c in sorted(schema.get(target, []), key=lambda c: c["pk"])
                        if c["pk"]
                    ]
                    names = [
                        f["to"] or (target_pk[i] if i < len(target_pk) else "")
                        for i, f in enumerate(group)
                    ]
                    values = {
                        name: cell(raw[f["from"]]) for name, f in zip(names, group, strict=True)
                    }
                    ref = (
                        lookup.get(
                            (
                                target,
                                tuple(
                                    (k, canonical_bytes(values[k]))
                                    for k in target_pk
                                    if k in values
                                ),
                            )
                        )
                        if set(values) == set(target_pk)
                        else None
                    )
                    if ref is None:
                        unresolved.append(
                            {
                                "code": "unresolved_reference",
                                "table": table,
                                "key": row["key"],
                                "target_table": target,
                            }
                        )
                    else:
                        row["references"].append(ref)
        # Polymorphic universal provenance is not a foreign key. Never quietly
        # count it reconciled without resolving both typed ends.
        types = {
            "scope": "lw_scopes",
            "evidence": "lw_evidence",
            "memory": "lw_memories",
            "procedure": "lw_procedures",
        }
        for row, raw in zip(
            [r for r in rows if r["table"] == "lw_links"], raw_rows.get("lw_links", []), strict=True
        ):
            for end in ("subject", "object"):
                target = types.get(str(raw.get(end + "_type", "")), "")
                ref = lookup.get((target, (("id", canonical_bytes(cell(raw.get(end + "_id")))),)))
                if ref is None:
                    unresolved.append(
                        {"code": "unresolved_typed_link", "key": row["key"], "end": end}
                    )
                else:
                    row["references"].append(ref)
        # A dependent cannot resurrect retired content from its copied body.
        dependents: dict[str, list[str]] = {}
        for row in rows:
            for ref in row["references"]:
                dependents.setdefault(ref, []).append(row["key"])
        retired_queue = deque(row["key"] for row in rows if row["retired"])
        while retired_queue:
            for dependent_key in dependents.get(retired_queue.popleft(), []):
                if not row_map[dependent_key]["retired"]:
                    row_map[dependent_key]["retired"] = True
                    retired_queue.append(dependent_key)
        if len({r["key"] for r in rows}) != len(rows):
            unresolved.append({"code": "duplicate_stable_key"})
        return {
            "version": 1,
            "source": source,
            "snapshot_sha256": before,
            "snapshot_bytes": info.st_size,
            "profiles": profiles,
            "schema_sha256": canonical_hash(schema),
            "tables": counts,
            "derived_tables_not_imported": derived,
            "rows": rows,
            "unresolved": unresolved,
        }
    except sqlite3.Error as exc:
        raise LocalError("legacy_sqlite_inspection_failed") from exc
    finally:
        db.close()
        if digest_file(path) != before:
            raise LocalError("legacy_source_changed")
