"""Team views for the shared profile: who did what in a project, and an audit export.

Both are bounded reads over recorded facts (events, retained artifacts, egress audit,
grants, summaries) under the caller's current authorization. Nothing here infers
intent, cost or success; a count is a count of records.
"""

from __future__ import annotations

import json
from typing import Any

from memoryking.adapters.local_store import identifier
from memoryking.adapters.timeline import timeline
from memoryking.application.store_core import StoreCore
from memoryking.domain.local import temporal_valid

DAY_MS = 86_400_000
EVENT_SCAN = 5000
EXPORT_EVENTS = 5000
EXPORT_ROWS = 2000


FAMILIES = {
    "capture": "captures",
    "observation": "captures",
    "checkpoint": "checkpoints",
    "handoff": "handoffs",
    "propose": "claims",
    "review": "claims",
    "attest": "claims",
    "promote": "claims",
    "claim": "claims",
    "revision": "claims",
    "summary": "summaries",
    "inference": "dispatches",
    "egress": "dispatches",
    "purge": "purges",
    "grant": "policy",
    "revoke": "policy",
    "policy": "policy",
    "experience": "procedures",
}


def _family(kind: str) -> str:
    return FAMILIES.get(kind.split("_", 1)[0], "other")


def _project_grants(store: StoreCore, tenant: str, project: str, at: int) -> dict[str, list[str]]:
    grants: dict[str, list[str]] = {}
    for row in store.db.execute(
        "SELECT principal,project,capabilities,starts,expires FROM grants WHERE tenant=? AND project IN (?,'*') ORDER BY principal",
        (tenant, project),
    ).fetchall():
        if not temporal_valid(at, row["starts"], row["expires"]):
            continue
        capabilities = row["capabilities"]
        if isinstance(capabilities, str):
            capabilities = json.loads(capabilities)
        current = set(grants.get(row["principal"], []))
        current.update(capabilities)
        grants[row["principal"]] = sorted(current)
    return grants


def activity(
    store: StoreCore, who: dict[str, Any], project: str, at: int, *, days: int = 30
) -> dict[str, Any]:
    """Per-principal record counts for a project over a window, newest events first."""
    identifier(project)
    days = max(1, min(days, 365))
    tenant, since = who["tenant"], at - days * DAY_MS
    rows = store.db.execute(
        "SELECT e.kind,e.actor,e.at_ms,"
        " o.project AS observation_project, f.project AS claim_project,"
        " c.project AS checkpoint_project, h.project AS handoff_project"
        " FROM events e"
        " LEFT JOIN observations o ON o.tenant=e.tenant AND o.id=e.subject"
        " LEFT JOIN revisions r ON r.tenant=e.tenant AND r.id=e.subject"
        " LEFT JOIN families f ON f.tenant=r.tenant AND f.id=r.family"
        " LEFT JOIN checkpoints c ON c.tenant=e.tenant AND c.id=e.subject"
        " LEFT JOIN handoffs h ON h.tenant=e.tenant AND h.id=e.subject"
        " WHERE e.tenant=? AND e.at_ms>=? ORDER BY e.position DESC LIMIT ?",
        (tenant, since, EVENT_SCAN + 1),
    ).fetchall()
    principals: dict[str, dict[str, Any]] = {}

    def entry(principal: str) -> dict[str, Any]:
        return principals.setdefault(
            principal,
            {
                "principal": principal,
                "grants": [],
                "events": {},
                "briefings": 0,
                "dispatches_delivered": 0,
                "last_seen_ms": None,
            },
        )

    for row in rows[:EVENT_SCAN]:
        if project not in (
            row["observation_project"],
            row["claim_project"],
            row["checkpoint_project"],
            row["handoff_project"],
        ):
            continue
        item = entry(row["actor"])
        family = _family(row["kind"])
        item["events"][family] = item["events"].get(family, 0) + 1
        item["last_seen_ms"] = max(item["last_seen_ms"] or 0, row["at_ms"])
    for row in store.db.execute(
        "SELECT principal,count(*) AS n,max(created) AS last FROM rendered_artifacts WHERE tenant=? AND project=? AND created>=? GROUP BY principal",
        (tenant, project, since),
    ).fetchall():
        item = entry(row["principal"])
        item["briefings"] = int(row["n"])
        item["last_seen_ms"] = max(item["last_seen_ms"] or 0, int(row["last"]))
    for row in store.db.execute(
        "SELECT principal,count(*) AS n,max(evaluated) AS last FROM egress_audit WHERE tenant=? AND project=? AND evaluated>=? AND status='delivered' GROUP BY principal",
        (tenant, project, since),
    ).fetchall():
        item = entry(row["principal"])
        item["dispatches_delivered"] = int(row["n"])
        item["last_seen_ms"] = max(item["last_seen_ms"] or 0, int(row["last"]))
    for principal, capabilities in _project_grants(store, tenant, project, at).items():
        entry(principal)["grants"] = capabilities
    ordered = sorted(principals.values(), key=lambda p: (-(p["last_seen_ms"] or 0), p["principal"]))
    return {
        "project": project,
        "since_ms": since,
        "until_ms": at,
        "days": days,
        "principals": ordered,
        "events_scanned": min(len(rows), EVENT_SCAN),
        "complete": len(rows) <= EVENT_SCAN,
    }


def export(store: StoreCore, who: dict[str, Any], project: str, at: int) -> dict[str, Any]:
    """A bounded, content-free audit document for a project under the caller's authority.

    Events, egress records, retained-artifact identities, summaries and grants; never
    observation text, claim bodies or artifact bodies. The caller must hold admin.
    """
    identifier(project)
    tenant = who["tenant"]
    truncated: list[str] = []
    events: list[dict[str, Any]] = []
    before: int | None = None
    while len(events) < EXPORT_EVENTS:
        page = timeline(store, who, project, before=before)
        events.extend(page["items"])
        if page["next_before"] is None:
            break
        before = page["next_before"]
    if len(events) > EXPORT_EVENTS:
        events = events[:EXPORT_EVENTS]
        truncated.append("events")
    egress = [
        {
            "permit": row["id"],
            "principal": row["principal"],
            "provider": row["provider"],
            "model": row["model"],
            "host": row["host"],
            "policy_revision": row["policy_revision"],
            "status": row["status"],
            "classification": row["classification"],
            "request_bytes": row["request_bytes"],
            "request_hash": row["request_hash"],
            "manifest_hash": row["manifest_hash"],
            "wire_hash": row["wire_hash"],
            "response_hash": row["response_hash"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "evaluated_ms": row["evaluated"],
            "reported_ms": row["reported"],
        }
        for row in store.db.execute(
            "SELECT * FROM egress_audit WHERE tenant=? AND project=? ORDER BY evaluated DESC,id DESC LIMIT ?",
            (tenant, project, EXPORT_ROWS + 1),
        ).fetchall()
    ]
    if len(egress) > EXPORT_ROWS:
        egress = egress[:EXPORT_ROWS]
        truncated.append("egress")
    artifacts = []
    for row in store.db.execute(
        "SELECT id,principal,created,expires,state,body_hash,diagnostics FROM rendered_artifacts WHERE tenant=? AND project=? ORDER BY created DESC,id DESC LIMIT ?",
        (tenant, project, EXPORT_ROWS + 1),
    ).fetchall():
        diag = json.loads(row["diagnostics"]) if row["diagnostics"] else {}
        artifacts.append(
            {
                "receipt": row["id"],
                "principal": row["principal"],
                "kind": diag.get("kind", "briefing"),
                "status": diag.get("status"),
                "state": row["state"],
                "created_ms": row["created"],
                "expires_ms": row["expires"],
                "rendered_hash": row["body_hash"],
            }
        )
    if len(artifacts) > EXPORT_ROWS:
        artifacts = artifacts[:EXPORT_ROWS]
        truncated.append("artifacts")
    summaries = [
        {
            "receipt": row["id"],
            "task": row["task"],
            "run": None if row["stream"] == "task" else row["stream"][4:],
            "checkpoint": row["checkpoint"],
            "revision": row["revision"],
            "principal": row["principal"],
            "method": row["method"],
            "created_ms": row["created"],
        }
        for row in store.db.execute(
            "SELECT * FROM session_summaries WHERE tenant=? AND project=? ORDER BY created DESC,id DESC LIMIT ?",
            (tenant, project, EXPORT_ROWS + 1),
        ).fetchall()
    ]
    if len(summaries) > EXPORT_ROWS:
        summaries = summaries[:EXPORT_ROWS]
        truncated.append("summaries")
    return {
        "schema": "audit-export-v1",
        "store": store.store_id,
        "tenant": tenant,
        "incarnation": store.incarnation,
        "project": project,
        "generated_ms": at,
        "generated_by": who["principal"],
        "grants": [
            {"principal": principal, "capabilities": capabilities}
            for principal, capabilities in _project_grants(store, tenant, project, at).items()
        ],
        "events": events,
        "egress": egress,
        "artifacts": artifacts,
        "summaries": summaries,
        "bounds": {"events": EXPORT_EVENTS, "rows": EXPORT_ROWS},
        "truncated": truncated,
        "content": "none: identities, hashes, kinds and instants only",
    }
