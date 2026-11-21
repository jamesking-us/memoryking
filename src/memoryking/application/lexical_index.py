"""The lexical index and its statistics, written once in portable SQL for both backends.

Three things are kept in step: the full-text `lexical` table (index text from
`domain.lexicon`), and per-project statistics (`lexical_projects`: documents and total
length; `lexical_terms`: documents per index term). Statistics describe the whole project,
held observations included, and never what one reader may see; they are read only to weigh
terms and to order candidate searches, and no packet or trace carries a term's count.

`lexical_state` says which format a tenant's index is in:

* current   - index text, statistics and capture, purge all in one format: lexical-v3
               (one statistics document per passage) or the earlier lexical-v2 (one per
               whole observation), which is kept working until a `reindex`
* rebuilding - a `reindex` is rewriting rows in batches; statistics are partial and are
               not used for ranking yet (briefings keep the previous ranking meanwhile)
* legacy    - rows were indexed as raw text before this format; they stay searchable by
               exact token and briefings keep the previous ranking until `reindex`

In lexical-v3 the index row of an observation is its passages' index texts joined by a
newline (a separator no analyzer treats as a word), so purge can undo each passage's
contribution from the stored row alone. A tenant with no row that already holds
observations is legacy; one without observations is current. While rebuilding, `cursor` is the position of the last observation rewritten
(-1 while old statistics are being cleared); an observation at or below it is counted in
the statistics, one above it is not yet, so capture and purge stay exact at any moment.
"""

from __future__ import annotations

import hashlib
import time
from collections import Counter
from typing import Any

from memoryking.application.storage import insert_ignore
from memoryking.domain.anchors import extract
from memoryking.domain.lexicon import (
    INDEX_CONFIGURATION,
    PREVIOUS_CONFIGURATION,
    Corpus,
    index_terms,
    index_text,
    plain_text,
)
from memoryking.domain.passages import split

CURRENT, REBUILDING, LEGACY = "current", "rebuilding", "legacy"
PASSAGE_BREAK = "\n"
CHUNK = 200  # index terms per statement
BATCH_DOCUMENTS = 64  # observations rewritten per step before the clock is consulted
STEP_MS = 1000  # one rebuild step stops after about this long, well inside the deadline


def state(db: Any, tenant: str) -> tuple[str, int]:
    row = db.execute("SELECT state,cursor FROM lexical_state WHERE tenant=?", (tenant,)).fetchone()
    if row is not None:
        return str(row[0]), int(row[1])
    held = db.execute("SELECT 1 FROM observations WHERE tenant=? LIMIT 1", (tenant,)).fetchone()
    return (LEGACY, 0) if held is not None else (CURRENT, 0)


def configuration(db: Any, tenant: str) -> str:
    """The configuration whose ranking a briefing uses now: lexical-v3 or lexical-v2 for
    a current tenant, lexical-v1 while the index is legacy or being rebuilt."""
    found, _ = state(db, tenant)
    if found != CURRENT:
        return "lexical-v1"
    row = db.execute("SELECT configuration FROM lexical_state WHERE tenant=?", (tenant,)).fetchone()
    return (
        str(row[0]) if row is not None and row[0] == PREVIOUS_CONFIGURATION else INDEX_CONFIGURATION
    )


def ensure(db: Any, tenant: str) -> None:
    """Record the tenant's state before its first row in this format is written."""
    found, _ = state(db, tenant)
    db.execute(
        insert_ignore("lexical_state", 4),
        (tenant, found, 0, INDEX_CONFIGURATION if found == CURRENT else "lexical-v1"),
    )


def build(text: str, passages: bool = True) -> tuple[str, list[str]]:
    """The index row for a text and the index text of each statistics document in it.

    lexical-v3 documents are the text's passages (those with at least one word); a
    lexical-v2 row is one document, the whole text, empty or not."""
    if not passages:
        row = index_text(text)
        return row, [row]
    parts = [index_text(text[p.start : p.end]) for p in split(text)]
    parts = [part for part in parts if part]
    return PASSAGE_BREAK.join(parts), parts


def _count(db: Any, tenant: str, project: str, parts: list[str], sign: int) -> None:
    """Add or take back the statistics of one observation's documents, in a few statements."""
    documents, length = len(parts), 0
    frequency: Counter[str] = Counter()
    for part in parts:
        terms, size = index_terms(part)
        length += size
        frequency.update(terms)
    if documents == 0:
        return
    if sign > 0:
        db.execute(
            "INSERT INTO lexical_projects(tenant,project,documents,tokens) VALUES (?,?,?,?) "
            "ON CONFLICT (tenant,project) DO UPDATE SET documents=lexical_projects.documents+EXCLUDED.documents, "
            "tokens=lexical_projects.tokens+EXCLUDED.tokens",
            (tenant, project, documents, length),
        )
    else:
        db.execute(
            "UPDATE lexical_projects SET documents=documents-?,tokens=tokens-? WHERE tenant=? AND project=?",
            (documents, length, tenant, project),
        )
    by_count: dict[int, list[str]] = {}
    for term, number in frequency.items():
        by_count.setdefault(number, []).append(term)
    for number, terms in sorted(by_count.items()):
        for start in range(0, len(terms), CHUNK):
            chunk = terms[start : start + CHUNK]
            if sign > 0:
                db.execute(
                    "INSERT INTO lexical_terms(tenant,project,term,documents) VALUES "
                    + ",".join("(?,?,?,?)" for _ in chunk)
                    + " ON CONFLICT (tenant,project,term) DO UPDATE SET documents=lexical_terms.documents+EXCLUDED.documents",
                    [value for term in chunk for value in (tenant, project, term, number)],
                )
            else:
                marks = ",".join("?" for _ in chunk)
                db.execute(
                    "UPDATE lexical_terms SET documents=documents-? WHERE tenant=? AND project=? AND term IN ("
                    + marks
                    + ")",
                    (number, tenant, project, *chunk),
                )
                db.execute(
                    "DELETE FROM lexical_terms WHERE tenant=? AND project=? AND documents<=0 AND term IN ("
                    + marks
                    + ")",
                    (tenant, project, *chunk),
                )


def add(db: Any, tenant: str, project: str, observation: str, text: str) -> None:
    """Index a newly captured observation; count it when the statistics are live."""
    current, _ = state(db, tenant)
    # A tenant still in the earlier format keeps writing it until its reindex.
    passages = not (current == CURRENT and configuration(db, tenant) == PREVIOUS_CONFIGURATION)
    row, parts = build(text, passages)
    db.execute("INSERT INTO lexical VALUES (?,?,?)", (tenant, observation, row))
    if current == CURRENT:
        _count(db, tenant, project, parts, 1)


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
        stored = str(text[0])
        if current == CURRENT and configuration(db, tenant) == PREVIOUS_CONFIGURATION:
            parts = [stored]
        else:
            parts = [part for part in stored.split(PASSAGE_BREAK) if part.strip()]
        _count(db, tenant, row["project"], parts, -1)


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


def count_observations(db: Any, tenant: str) -> int:
    return int(
        db.execute(
            "SELECT count(*) FROM observations WHERE tenant=? AND deleted=0", (tenant,)
        ).fetchone()[0]
    )


def describe(db: Any, tenant: str) -> dict[str, Any]:
    current, cursor = state(db, tenant)
    result: dict[str, Any] = {"state": current, "configuration": configuration(db, tenant)}
    if result["configuration"] != INDEX_CONFIGURATION:
        # the ranking in use until the rebuild finishes
        result["target_configuration"] = INDEX_CONFIGURATION
        result["remaining_observations"] = (
            remaining(db, tenant) if current != CURRENT else count_observations(db, tenant)
        )
        result["action"] = (
            "run reindex to rebuild the lexical index; the daemon finishes it in batches"
        )
    return result


def step(db: Any, tenant: str, text_of: Any, budget_ms: int = STEP_MS) -> bool:
    """One bounded unit of the rebuild; True when the tenant's index is current.

    `text_of(row)` returns an observation's retained text or None when it cannot be read."""
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
        indexed, parts = build(text)
        done.append((tenant, row["id"], indexed))
        db.execute("DELETE FROM anchors WHERE tenant=? AND id=?", (tenant, row["id"]))
        for kind, anchor in extract(text):
            db.execute(insert_ignore("anchors", 4), (tenant, row["id"], anchor, kind))
        _count(db, tenant, row["project"], parts, 1)
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
    db.execute("UPDATE lexical_state SET cursor=? WHERE tenant=?", (last, tenant))
    return False


def blob_text(root: Any, row: Any, tenant: str) -> str | None:
    """The retained text of an observation, verified against its content hash."""
    from memoryking.adapters.local_files import private_file
    from memoryking.domain.local import LocalError

    if row["blob"] is None:
        return None
    path = root / "blobs" / tenant / row["blob"]
    try:
        private_file(path)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != row["blob"]:
            return None
        text: str = raw.decode()
        return text
    except (OSError, LocalError, UnicodeDecodeError):
        return None
