"""A project's event timeline: captures, claims, reviews, checkpoints, handoffs and purges."""

from __future__ import annotations

from typing import Any

from memoryking.adapters.local_store import identifier
from memoryking.application.store_core import StoreCore

LIMIT = 100


def timeline(
    store: StoreCore,
    who: dict[str, Any],
    project: str,
    *,
    before: int | None = None,
    limit: int = LIMIT,
) -> dict[str, Any]:
    """Newest-first events whose subject belongs to the project, with a paging cursor.

    Events carry no project themselves; a subject is resolved through the observation,
    claim, checkpoint or handoff it names. Tenant-wide events such as grants are shown
    only to principals with a tenant-wide grant, under the subject kind `tenant`.
    """
    identifier(project)
    limit = max(1, min(limit, LIMIT))
    tenant = who["tenant"]
    params: list[Any] = [tenant]
    cursor = ""
    if before is not None:
        cursor = " AND e.position<?"
        params.append(before)
    rows = store.db.execute(
        "SELECT e.position,e.kind,e.subject,e.actor,e.at_ms,"
        " o.project AS observation_project,"
        " f.project AS claim_project,"
        " c.project AS checkpoint_project,"
        " h.project AS handoff_project"
        " FROM events e"
        " LEFT JOIN observations o ON o.tenant=e.tenant AND o.id=e.subject"
        " LEFT JOIN revisions r ON r.tenant=e.tenant AND r.id=e.subject"
        " LEFT JOIN families f ON f.tenant=r.tenant AND f.id=r.family"
        " LEFT JOIN checkpoints c ON c.tenant=e.tenant AND c.id=e.subject"
        " LEFT JOIN handoffs h ON h.tenant=e.tenant AND h.id=e.subject"
        f" WHERE e.tenant=?{cursor} ORDER BY e.position DESC LIMIT ?",
        (*params, limit * 4),
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        kinds = (
            ("observation", row["observation_project"]),
            ("claim", row["claim_project"]),
            ("checkpoint", row["checkpoint_project"]),
            ("handoff", row["handoff_project"]),
        )
        subject_kind = next((k for k, p in kinds if p == project), None)
        if subject_kind is None:
            continue
        items.append(
            {
                "position": row["position"],
                "kind": row["kind"],
                "subject": row["subject"],
                "subject_kind": subject_kind,
                "actor": row["actor"],
                "at_ms": row["at_ms"],
            }
        )
        if len(items) >= limit:
            break
    scanned_to = rows[-1]["position"] if rows else None
    exhausted = len(rows) < limit * 4
    return {
        "project": project,
        "items": items,
        "next_before": None if exhausted and len(items) < limit else scanned_to,
    }
