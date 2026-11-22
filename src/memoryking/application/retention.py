"""Live counts and retention for the per-tenant bounds that hold history.

Checkpoints, hand-offs, hand-off views and experience records used to count every row they
ever wrote, so a tenant reached its bound for good. Each bound now counts only live rows,
and a retention rule keeps the rest small. Nothing here changes what a read verifies:

* A checkpoint stream keeps its latest revision and the 129 revisions before it, which is
  every revision a read of that stream inspects (an execution snapshot reads 128 prior
  revisions and treats a longer history as incomplete). Older revisions become tombstones:
  the row stays, so the revision number stays consumed and events still resolve to their
  project, but body and dependencies are gone. A structured plan keeps every revision,
  because completion evidence is verified against all earlier ones.
* A tombstone is never made for a checkpoint that a retained artifact, a session summary or
  a live hand-off still names, so retained receipts keep replaying.
* Hand-off rows are small fixed metadata that events resolve through; they stay and drop out
  of the count once cancelled, superseded, retired, purged or expired. A hand-off view is
  only usable while its artifact is retained, so views without one are deleted.
* An experience record keeps its latest revision per key (the consumed identity, even when
  purged) and the seven before it; older revisions that nothing depends on are deleted.
  Records of kind `feedback` and `shadow` whose latest revision is older than a year and that
  nothing depends on lose their bodies like a purged record (the revision numbers stay
  consumed); other kinds never expire by age.
* Above 80 percent of the checkpoint bound the window of an ordinary stream shrinks to its
  newest 32 revisions. A stream that released revisions reports `history_complete: false`
  in execution snapshots, so proof that rested on them is verified again, never assumed.
  Structured plans keep every revision at any pressure.
* Completed and no-op jobs behind the projection frontier are deleted (the newest 512 stay
  for the operations view); a reindex or restore creates the jobs it needs again from the
  event history. Events, idempotency tombstones (`retries` rows keep their request hash so a
  reused key is still refused) and operation intents are history and stay. Artifact
  tombstones (expired, evicted, purged, retired) go 30 days after they expired; access
  receipts already expire after 30 days.
"""

from __future__ import annotations

import json
import time
from typing import Any

CHECKPOINT_LIMIT = 4096
CHECKPOINT_BYTES_LIMIT = 268435456
HANDOFF_LIMIT = 4096
HANDOFF_VIEW_LIMIT = 4096
EXPERIENCE_LIMIT = 10000
CHECKPOINT_WINDOW = 129
# At 80 percent of the checkpoint bound an ordinary stream keeps its newest 32 revisions:
# the latest and 31 before it.
CHECKPOINT_PRESSURE_WINDOW = 31
EXPERIENCE_WINDOW = 8
EXPERIENCE_AGE_MS = 365 * 24 * 60 * 60 * 1000
EXPERIENCE_EXPIRING_KINDS = ("feedback", "shadow")
JOB_KEEP = 512
TOMBSTONE_GRACE_MS = 30 * 24 * 60 * 60 * 1000
LIVE_HANDOFF_STATES = "('prepared','acknowledged')"

_RETAINED_VIEW = (
    "EXISTS(SELECT 1 FROM rendered_artifacts a WHERE a.tenant=v.tenant AND a.id=v.id"
    " AND a.state='retained')"
)


def live_checkpoints(db: Any, dialect: Any, tenant: str) -> tuple[int, int]:
    """Available checkpoints and their body bytes; retired and purged rows hold no body."""
    count, size = db.execute(
        "SELECT count(*),coalesce(sum(" + dialect.byte_length("body") + "),0)"
        " FROM checkpoints WHERE tenant=? AND state='available'",
        (tenant,),
    ).fetchone()
    return int(count), int(size)


def live_handoffs(db: Any, tenant: str, at: int) -> int:
    return int(
        db.execute(
            "SELECT count(*) FROM handoffs WHERE tenant=? AND state IN "
            + LIVE_HANDOFF_STATES
            + " AND expires>?",
            (tenant, at),
        ).fetchone()[0]
    )


def live_handoff_views(db: Any, tenant: str) -> int:
    return int(
        db.execute(
            "SELECT count(*) FROM handoff_views v WHERE v.tenant=? AND " + _RETAINED_VIEW,
            (tenant,),
        ).fetchone()[0]
    )


def live_experience(db: Any, tenant: str, incarnation: str) -> int:
    """Latest revision of each record key in this history that still has its body."""
    return int(
        db.execute(
            "SELECT count(*) FROM (SELECT body,row_number() OVER (PARTITION BY project,"
            "principal,kind,logical_key ORDER BY revision DESC) AS newer FROM experience_records"
            " WHERE tenant=? AND incarnation=?) ranked WHERE newer=1 AND body IS NOT NULL",
            (tenant, incarnation),
        ).fetchone()[0]
    )


def _ids(raw: Any) -> set[str]:
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return set()
    return {i["id"] for i in items if isinstance(i, dict) and isinstance(i.get("id"), str)}


def _cited(db: Any, tenant: str) -> set[str]:
    """Identifiers named by the dependencies of every retained artifact."""
    cited: set[str] = set()
    for row in db.execute(
        "SELECT dependencies FROM rendered_artifacts WHERE tenant=? AND state='retained'",
        (tenant,),
    ).fetchall():
        cited |= _ids(row["dependencies"])
    return cited


def release_checkpoints(db: Any, dialect: Any, tenant: str) -> int:
    """Retire revisions older than the window of ordinary streams; plans keep all of theirs.

    Above 80 percent of the bound the window is the newest 32 revisions instead of 130."""
    count, _ = live_checkpoints(db, dialect, tenant)
    window = CHECKPOINT_PRESSURE_WINDOW if count * 5 >= CHECKPOINT_LIMIT * 4 else CHECKPOINT_WINDOW
    rows = db.execute(
        "SELECT c.id,c.project,c.task,c.stream,s.latest FROM checkpoints c"
        " JOIN checkpoint_streams s ON s.tenant=c.tenant AND s.project=c.project"
        " AND s.task=c.task AND s.stream=c.stream"
        " WHERE c.tenant=? AND c.state='available' AND c.revision<s.revision-?"
        " AND c.id<>s.latest ORDER BY c.position",
        (tenant, window),
    ).fetchall()
    if not rows:
        return 0
    protected = _cited(db, tenant)
    protected.update(
        r["checkpoint"]
        for r in db.execute("SELECT checkpoint FROM session_summaries WHERE tenant=?", (tenant,))
    )
    protected.update(
        r["checkpoint"]
        for r in db.execute(
            "SELECT checkpoint FROM handoffs WHERE tenant=? AND state IN " + LIVE_HANDOFF_STATES,
            (tenant,),
        )
    )
    ordinary: dict[str, bool] = {}
    retired = 0
    for row in rows:
        if row["id"] in protected:
            continue
        if row["latest"] not in ordinary:
            latest = db.execute(
                "SELECT state,body FROM checkpoints WHERE tenant=? AND id=?",
                (tenant, row["latest"]),
            ).fetchone()
            # An unreadable latest revision leaves the stream's type unknown: keep it all.
            ordinary[row["latest"]] = (
                latest is not None
                and latest["state"] == "available"
                and latest["body"] is not None
                and "plan" not in json.loads(latest["body"])
            )
        if ordinary[row["latest"]]:
            db.execute(
                "UPDATE checkpoints SET state='retired',body=NULL,dependencies='[]'"
                " WHERE tenant=? AND id=?",
                (tenant, row["id"]),
            )
            retired += 1
    return retired


def release_handoff_views(db: Any, tenant: str) -> int:
    """Delete views whose artifact is gone: they can no longer be acknowledged or replayed."""
    return int(
        db.execute(
            "DELETE FROM handoff_views WHERE tenant=? AND NOT EXISTS(SELECT 1 FROM"
            " rendered_artifacts a WHERE a.tenant=handoff_views.tenant"
            " AND a.id=handoff_views.id AND a.state='retained')",
            (tenant,),
        ).rowcount
    )


def release_experience(db: Any, tenant: str, incarnation: str) -> int:
    """Delete superseded revisions beyond the window that no record, artifact or ranking uses."""
    rows = db.execute(
        "SELECT id FROM (SELECT id,row_number() OVER (PARTITION BY project,principal,kind,"
        "logical_key ORDER BY revision DESC) AS newer FROM experience_records"
        " WHERE tenant=? AND incarnation=?) ranked WHERE newer>?",
        (tenant, incarnation, EXPERIENCE_WINDOW),
    ).fetchall()
    if not rows:
        return 0
    protected = _experience_protected(db, tenant)
    deleted = 0
    for row in rows:
        if row["id"] not in protected:
            db.execute(
                "DELETE FROM experience_records WHERE tenant=? AND id=?", (tenant, row["id"])
            )
            deleted += 1
    return deleted


def _experience_protected(db: Any, tenant: str) -> set[str]:
    """Experience records that an artifact, another record or a ranking configuration names."""
    protected = _cited(db, tenant)
    for row in db.execute(
        "SELECT dependencies FROM experience_records WHERE tenant=? AND body IS NOT NULL",
        (tenant,),
    ).fetchall():
        protected |= _ids(row["dependencies"])
    protected.update(
        r[0]
        for r in db.execute(
            "SELECT configuration FROM ranking_state WHERE tenant=? AND configuration IS NOT NULL"
            " UNION SELECT configuration FROM ranking_history WHERE tenant=?"
            " AND configuration IS NOT NULL",
            (tenant, tenant),
        )
    )
    return protected


def expire_experience(db: Any, tenant: str, incarnation: str, now: int) -> int:
    """Drop the bodies of feedback and shadow records whose latest revision is over a year old.

    The rows stay, so the key's revision numbers stay consumed and events still resolve;
    only records that no artifact, record or ranking configuration names expire."""
    kinds = ",".join("'" + k + "'" for k in EXPERIENCE_EXPIRING_KINDS)
    old = db.execute(
        "SELECT id,project,principal,kind,logical_key FROM (SELECT e.id,e.project,e.principal,"
        "e.kind,e.logical_key,e.body,v.at_ms,row_number() OVER (PARTITION BY e.project,"
        "e.principal,e.kind,e.logical_key ORDER BY e.revision DESC) AS newer"
        " FROM experience_records e JOIN events v ON v.tenant=e.tenant AND v.position=e.position"
        " WHERE e.tenant=? AND e.incarnation=? AND e.kind IN (" + kinds + ")) ranked"
        " WHERE newer=1 AND body IS NOT NULL AND at_ms<=?",
        (tenant, incarnation, now - EXPERIENCE_AGE_MS),
    ).fetchall()
    if not old:
        return 0
    protected = _experience_protected(db, tenant)
    expired = 0
    for latest in old:
        if latest["id"] in protected:
            continue
        for row in db.execute(
            "SELECT id FROM experience_records WHERE tenant=? AND incarnation=? AND project=?"
            " AND principal=? AND kind=? AND logical_key=? AND body IS NOT NULL",
            (
                tenant,
                incarnation,
                latest["project"],
                latest["principal"],
                latest["kind"],
                latest["logical_key"],
            ),
        ).fetchall():
            if row["id"] not in protected:
                db.execute(
                    "UPDATE experience_records SET body=NULL,dependencies='[]',readers=NULL"
                    " WHERE tenant=? AND id=?",
                    (tenant, row["id"]),
                )
                expired += 1
    return expired


def release_jobs(db: Any, tenant: str, chunk: int = 10000, budget_s: float = 0.5) -> int:
    """Delete completed and no-op jobs the projection frontier has passed, oldest first.

    The newest 512 stay for the operations view. Work is bounded by position windows and a
    time budget; the rest waits for the next pass."""
    row = db.execute("SELECT frontier FROM projections WHERE tenant=?", (tenant,)).fetchone()
    if row is None:
        return 0
    limit = row[0] - JOB_KEEP
    started, deleted = time.monotonic(), 0
    while limit > 0 and time.monotonic() - started < budget_s:
        low = db.execute(
            "SELECT position FROM jobs WHERE tenant=? AND state IN ('complete','noop')"
            " ORDER BY position LIMIT 1",
            (tenant,),
        ).fetchone()
        if low is None or low[0] > limit:
            break
        deleted += int(
            db.execute(
                "DELETE FROM jobs WHERE tenant=? AND position>=? AND position<? AND position<=?"
                " AND state IN ('complete','noop')",
                (tenant, low[0], low[0] + chunk, limit),
            ).rowcount
        )
    return deleted


def _chunked(db: Any, statement: str, params: tuple[Any, ...], chunk: int, budget_s: float) -> int:
    """Run a statement whose last parameter is a LIMIT until it removes fewer rows than that."""
    started, total = time.monotonic(), 0
    while True:
        changed = int(db.execute(statement, (*params, chunk)).rowcount)
        total += changed
        if changed < chunk or time.monotonic() - started >= budget_s:
            return total


def expire_retries(
    db: Any, tenant: str, now: int, chunk: int = 10000, budget_s: float = 0.5
) -> int:
    """Drop expired replay results; the row stays as the key's consumed identity."""
    return _chunked(
        db,
        "UPDATE retries SET result=NULL WHERE tenant=? AND result IS NOT NULL AND expires<=?"
        " AND (incarnation,namespace,principal,operation,key) IN (SELECT incarnation,namespace,"
        "principal,operation,key FROM retries WHERE tenant=? AND result IS NOT NULL AND expires<=?"
        " LIMIT ?)",
        (tenant, now, tenant, now),
        chunk,
        budget_s,
    )


def release_receipts(
    db: Any, tenant: str, before: int, chunk: int = 10000, budget_s: float = 0.5
) -> int:
    """Delete access receipts evaluated before `before` (and their observation lists)."""
    return _chunked(
        db,
        "DELETE FROM receipts WHERE tenant=? AND id IN (SELECT id FROM receipts WHERE tenant=?"
        " AND evaluated<=? LIMIT ?)",
        (tenant, tenant, before),
        chunk,
        budget_s,
    )


def release_artifacts(
    db: Any, tenant: str, now: int, chunk: int = 10000, budget_s: float = 0.5
) -> int:
    """Delete artifact tombstones 30 days after they expired; retained artifacts never."""
    return _chunked(
        db,
        "DELETE FROM rendered_artifacts WHERE tenant=? AND id IN (SELECT id FROM"
        " rendered_artifacts WHERE tenant=? AND state<>'retained' AND expires<=? LIMIT ?)",
        (tenant, tenant, now - TOMBSTONE_GRACE_MS),
        chunk,
        budget_s,
    )


def index_receipt(db: Any, tenant: str, receipt: str) -> None:
    """List the observations a receipt names in the join table purge uses to find it."""
    db.execute(
        "INSERT INTO receipt_observations(tenant,receipt,observation)"
        " SELECT DISTINCT r.tenant,r.id,j.value FROM receipts r, json_each(r.observation_ids) j"
        " WHERE r.tenant=? AND r.id=? AND r.observation_ids<>'[]' ON CONFLICT DO NOTHING",
        (tenant, receipt),
    )


def release_all(db: Any, dialect: Any, tenant: str, incarnation: str, now: int) -> int:
    return (
        release_checkpoints(db, dialect, tenant)
        + release_handoff_views(db, tenant)
        + release_experience(db, tenant, incarnation)
        + expire_experience(db, tenant, incarnation, now)
        + release_jobs(db, tenant)
        + release_artifacts(db, tenant, now)
    )


# The record types whose size or age the functions above bound, with how: a window of revisions,
# a time to live, the projection frontier, or the artifact a row needs. The record-type registry
# declares the same set (tests/test_records.py asserts they agree), so a new type that holds
# history is not forgotten here.
RETENTION_RULES = {
    "checkpoints": "window",
    "experience_records": "window",
    "jobs": "frontier",
    "handoff_views": "orphan",
    "rendered_artifacts": "expire",
    "retries": "expire",
    "receipts": "expire",
    "processing_usage": "expire",
    "egress_audit": "expire",
    "session_summaries": "expire",
}
