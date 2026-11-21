"""The lexical index and its statistics, written once in portable SQL for both backends.

Three things are kept in step: the full-text `lexical` table (index text from
`domain.lexicon`), and per-project statistics (`lexical_projects`: documents and total
length; `lexical_terms`: documents per index term). Statistics describe the whole project,
held observations included, and never what one reader may see; they are read only to weigh
terms and to order candidate searches, and no packet or trace carries a term's count.

`lexical_state` says which format a tenant's index is in:

* current   - index text, statistics and capture, purge all in the lexical-v2 format
* rebuilding - a `reindex` is rewriting rows in batches; statistics are partial and are
               not used for ranking yet (briefings keep the previous ranking meanwhile)
* legacy    - rows were indexed as raw text before this format; they stay searchable by
               exact token and briefings keep the previous ranking until `reindex`

A tenant with no row that already holds observations is legacy; one without observations
is current. While rebuilding, `cursor` is the position of the last observation rewritten
(-1 while old statistics are being cleared); an observation at or below it is counted in
the statistics, one above it is not yet, so capture and purge stay exact at any moment.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from memoryking.application.storage import insert_ignore
from memoryking.domain.anchors import extract
from memoryking.domain.lexicon import (
    INDEX_CONFIGURATION,
    Corpus,
    index_terms,
    index_text,
    plain_text,
)

CURRENT, REBUILDING, LEGACY = "current", "rebuilding", "legacy"
CHUNK = 200  # index terms per statement
BATCH_DOCUMENTS = 64  # observations rewritten per step before the clock is consulted
STEP_MS = 1000  # one rebuild step stops after about this long, well inside the deadline
TAIL_POSITIONS = 20_000  # event positions past the last observation covered per step
RATE_PER_SECOND = (
    400  # observations rewritten per second on the reference host; the estimate's floor
)


def state(db: Any, tenant: str) -> tuple[str, int]:
    row = db.execute("SELECT state,cursor FROM lexical_state WHERE tenant=?", (tenant,)).fetchone()
    if row is not None:
        return str(row[0]), int(row[1])
    held = db.execute("SELECT 1 FROM observations WHERE tenant=? LIMIT 1", (tenant,)).fetchone()
    return (LEGACY, 0) if held is not None else (CURRENT, 0)


def ensure(db: Any, tenant: str) -> None:
    """Record the tenant's state before its first row in this format is written."""
    found, _ = state(db, tenant)
    db.execute(
        insert_ignore("lexical_state", 4),
        (tenant, found, 0, INDEX_CONFIGURATION if found == CURRENT else "lexical-v1"),
    )


def _count(db: Any, tenant: str, project: str, indexed: str, sign: int) -> None:
    terms, length = index_terms(indexed)
    if sign > 0:
        db.execute(
            "INSERT INTO lexical_projects(tenant,project,documents,tokens) VALUES (?,?,1,?) "
            "ON CONFLICT (tenant,project) DO UPDATE SET documents=lexical_projects.documents+1, "
            "tokens=lexical_projects.tokens+EXCLUDED.tokens",
            (tenant, project, length),
        )
    else:
        db.execute(
            "UPDATE lexical_projects SET documents=documents-1,tokens=tokens-? WHERE tenant=? AND project=?",
            (length, tenant, project),
        )
    for start in range(0, len(terms), CHUNK):
        chunk = terms[start : start + CHUNK]
        if sign > 0:
            db.execute(
                "INSERT INTO lexical_terms(tenant,project,term,documents) VALUES "
                + ",".join("(?,?,?,1)" for _ in chunk)
                + " ON CONFLICT (tenant,project,term) DO UPDATE SET documents=lexical_terms.documents+1",
                [value for term in chunk for value in (tenant, project, term)],
            )
        else:
            marks = ",".join("?" for _ in chunk)
            db.execute(
                "UPDATE lexical_terms SET documents=documents-1 WHERE tenant=? AND project=? AND term IN ("
                + marks
                + ")",
                (tenant, project, *chunk),
            )
            db.execute(
                "DELETE FROM lexical_terms WHERE tenant=? AND project=? AND documents<=0 AND term IN ("
                + marks
                + ")",
                (tenant, project, *chunk),
            )


def add(db: Any, tenant: str, project: str, observation: str, text: str) -> None:
    """Index a newly captured observation; count it when the statistics are live."""
    indexed = index_text(text)
    db.execute("INSERT INTO lexical VALUES (?,?,?)", (tenant, observation, indexed))
    if state(db, tenant)[0] == CURRENT:
        _count(db, tenant, project, indexed, 1)


def remove(db: Any, tenant: str, observation: str) -> None:
    """Drop a purged observation from the index and undo exactly what it contributed."""
    row = db.execute(
        "SELECT project,position FROM observations WHERE tenant=? AND id=?", (tenant, observation)
    ).fetchone()
    text = db.execute(
        "SELECT text FROM lexical WHERE tenant=? AND id=?", (tenant, observation)
    ).fetchone()
    db.execute("DELETE FROM lexical WHERE tenant=? AND id=?", (tenant, observation))
    if row is None or text is None:
        return
    current, cursor = state(db, tenant)
    counted = current == CURRENT or (current == REBUILDING and row["position"] <= cursor)
    if counted:
        _count(db, tenant, row["project"], str(text[0]), -1)


def corpus(db: Any, tenant: str, project: str, keys: list[str]) -> Corpus:
    row = db.execute(
        "SELECT documents,tokens FROM lexical_projects WHERE tenant=? AND project=?",
        (tenant, project),
    ).fetchone()
    return Corpus(
        int(row[0]) if row else 0, int(row[1]) if row else 0, frequencies(db, tenant, project, keys)
    )


def frequencies(db: Any, tenant: str, project: str, keys: list[str]) -> dict[str, int]:
    found: dict[str, int] = {}
    for start in range(0, len(keys), CHUNK):
        chunk = keys[start : start + CHUNK]
        for row in db.execute(
            "SELECT term,documents FROM lexical_terms WHERE tenant=? AND project=? AND term IN ("
            + ",".join("?" for _ in chunk)
            + ")",
            (tenant, project, *chunk),
        ).fetchall():
            found[str(row[0])] = int(row[1])
    return found


def begin(db: Any, tenant: str) -> None:
    """Start (or restart) a rebuild: statistics are cleared in the first steps, not here."""
    db.execute(
        "INSERT INTO lexical_state(tenant,state,cursor,configuration) VALUES (?,?,?,?) "
        "ON CONFLICT (tenant) DO UPDATE SET state=EXCLUDED.state,cursor=EXCLUDED.cursor,"
        "configuration=EXCLUDED.configuration",
        (tenant, REBUILDING, -1, INDEX_CONFIGURATION),
    )


def remaining(db: Any, tenant: str) -> int:
    current, cursor = state(db, tenant)
    if current == CURRENT:
        return 0
    return int(
        db.execute(
            "SELECT count(*) FROM observations WHERE tenant=? AND deleted=0 AND position>?",
            (tenant, max(cursor, 0)),
        ).fetchone()[0]
    )


def progress(db: Any, tenant: str, rate: float | None = None) -> dict[str, Any]:
    """Observations total, left, percent complete and a time estimate for a rebuild.

    `rate` is observations per second seen by this process; without a sample the reference
    rate is used, so the estimate is a rough guide, never a promise."""
    current, _ = state(db, tenant)
    if current == CURRENT:
        return {"percent_complete": 100, "remaining_observations": 0}
    total = int(
        db.execute(
            "SELECT count(*) FROM observations WHERE tenant=? AND deleted=0", (tenant,)
        ).fetchone()[0]
    )
    left = remaining(db, tenant)
    speed = rate if rate and rate > 0 else RATE_PER_SECOND
    return {
        "total_observations": total,
        "remaining_observations": left,
        "percent_complete": 100 if total == 0 else max(0, min(99, (total - left) * 100 // total)),
        "estimated_seconds": int(left / speed) + 1 if left else 0,
    }


def describe(
    db: Any, tenant: str, rate: float | None = None, counts: bool = True
) -> dict[str, Any]:
    current, cursor = state(db, tenant)
    result: dict[str, Any] = {"state": current, "configuration": INDEX_CONFIGURATION}
    if current != CURRENT:
        result["configuration"] = "lexical-v1"  # the ranking in use until the rebuild finishes
        result["target_configuration"] = INDEX_CONFIGURATION
        if counts:
            result.update(progress(db, tenant, rate))
        result["action"] = (
            "run `memoryking reindex` to rebuild the lexical index; the daemon finishes it in batches"
            if current == LEGACY
            else "rebuilding in batches; `memoryking reindex --wait` shows progress"
        )
    return result


def step(
    db: Any,
    tenant: str,
    text_of: Any,
    budget_ms: int = STEP_MS,
    reset: Any = None,
    stats: dict[str, int] | None = None,
) -> bool:
    """One bounded unit of the rebuild; True when the tenant's index is current.

    `text_of(row)` returns an observation's retained text or None when it cannot be read.
    `reset(low, high)`, when given, is called inside the same transaction with the event
    positions this step has now covered (`low` exclusive, `high` inclusive), so work that
    must follow the rebuild across the store (the projection jobs) is batched with it and
    resumes from the same cursor."""
    current, cursor = state(db, tenant)
    if current != REBUILDING:
        return current == CURRENT
    started = time.monotonic()
    if cursor < 0:
        while True:
            rows = db.execute(
                "SELECT project,term FROM lexical_terms WHERE tenant=? LIMIT ?", (tenant, 500)
            ).fetchall()
            if not rows:
                break
            for project, term in rows:
                db.execute(
                    "DELETE FROM lexical_terms WHERE tenant=? AND project=? AND term=?",
                    (tenant, project, term),
                )
            if (time.monotonic() - started) * 1000 > budget_ms:
                return False
        db.execute("DELETE FROM lexical_projects WHERE tenant=?", (tenant,))
        db.execute("UPDATE lexical_state SET cursor=0 WHERE tenant=?", (tenant,))
        return False
    rows = db.execute(
        "SELECT id,project,position,blob FROM observations WHERE tenant=? AND deleted=0 AND position>? ORDER BY position LIMIT ?",
        (tenant, cursor, BATCH_DOCUMENTS),
    ).fetchall()
    if not rows:
        if reset is not None:
            top = int(
                db.execute("SELECT position FROM tenants WHERE tenant=?", (tenant,)).fetchone()[0]
            )
            if cursor < top:  # events after the last observation, in bounded slices
                upper = min(top, cursor + TAIL_POSITIONS)
                reset(cursor, upper)
                db.execute("UPDATE lexical_state SET cursor=? WHERE tenant=?", (upper, tenant))
                return False
        db.execute("UPDATE lexical_state SET state=? WHERE tenant=?", (CURRENT, tenant))
        return True
    done: list[tuple[Any, ...]] = []
    last = cursor
    for row in rows:
        text = text_of(row)
        if text is None:
            old = db.execute(
                "SELECT text FROM lexical WHERE tenant=? AND id=?", (tenant, row["id"])
            ).fetchone()
            text = plain_text(str(old[0])) if old else ""
        indexed = index_text(text)
        done.append((tenant, row["id"], indexed))
        db.execute("DELETE FROM anchors WHERE tenant=? AND id=?", (tenant, row["id"]))
        for kind, anchor in extract(text):
            db.execute(insert_ignore("anchors", 4), (tenant, row["id"], anchor, kind))
        _count(db, tenant, row["project"], indexed, 1)
        last = row["position"]
        if (time.monotonic() - started) * 1000 > budget_ms:
            break
    ids = [d[1] for d in done]
    db.execute(
        "DELETE FROM lexical WHERE tenant=? AND id IN (" + ",".join("?" for _ in ids) + ")",
        (tenant, *ids),
    )
    for entry in done:
        db.execute("INSERT INTO lexical VALUES (?,?,?)", entry)
    if stats is not None:
        stats["documents"] = stats.get("documents", 0) + len(done)
    if reset is not None:
        reset(cursor, last)
    db.execute("UPDATE lexical_state SET cursor=? WHERE tenant=?", (last, tenant))
    return False


def blob_text(root: Any, row: Any, tenant: str) -> str | None:
    """The retained text of an observation, verified against its content hash."""
    from memoryking.adapters.blobs import blob_path
    from memoryking.adapters.local_files import private_file
    from memoryking.domain.local import LocalError

    if row["blob"] is None:
        return None
    path = blob_path(root, tenant, row["blob"])
    try:
        private_file(path)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != row["blob"]:
            return None
        text: str = raw.decode()
        return text
    except (OSError, LocalError, UnicodeDecodeError):
        return None
