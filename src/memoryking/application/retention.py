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
"""

from __future__ import annotations

import json
from typing import Any

CHECKPOINT_LIMIT = 4096
CHECKPOINT_BYTES_LIMIT = 268435456
HANDOFF_LIMIT = 4096
HANDOFF_VIEW_LIMIT = 4096
EXPERIENCE_LIMIT = 10000
CHECKPOINT_WINDOW = 129
EXPERIENCE_WINDOW = 8
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


def release_checkpoints(db: Any, tenant: str) -> int:
    """Retire revisions older than the window of ordinary streams; plans keep all of theirs."""
    rows = db.execute(
        "SELECT c.id,c.project,c.task,c.stream,s.latest FROM checkpoints c"
        " JOIN checkpoint_streams s ON s.tenant=c.tenant AND s.project=c.project"
        " AND s.task=c.task AND s.stream=c.stream"
        " WHERE c.tenant=? AND c.state='available' AND c.revision<s.revision-?"
        " AND c.id<>s.latest ORDER BY c.position",
        (tenant, CHECKPOINT_WINDOW),
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
    deleted = 0
    for row in rows:
        if row["id"] not in protected:
            db.execute(
                "DELETE FROM experience_records WHERE tenant=? AND id=?", (tenant, row["id"])
            )
            deleted += 1
    return deleted


def release_all(db: Any, tenant: str, incarnation: str) -> int:
    return (
        release_checkpoints(db, tenant)
        + release_handoff_views(db, tenant)
        + release_experience(db, tenant, incarnation)
    )
