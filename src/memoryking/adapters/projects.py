"""Projects a principal may currently read, with counts, for dashboards and CLIs."""

from __future__ import annotations

import json
from typing import Any

from memoryking.adapters.local_store import LocalStore, identifier
from memoryking.domain.local import temporal_valid


def visible_projects(store: LocalStore, credential: str, at: int) -> list[dict[str, Any]]:
    """Projects the credential can read now, with observation, claim, task and receipt counts.

    A tenant-wide grant lists every project that holds records; a scoped grant lists only
    its own project, whether or not it holds records yet. Counts are tenant-wide per
    project and describe presence, not what this principal may see of each record.
    """
    identity = store._decode(credential, "credential")
    tenant, principal = identity["tenant"], identity["principal"]
    db = store.db
    readable: set[str] = set()
    everything = False
    for row in db.execute(
        "SELECT project,capabilities,starts,expires FROM grants WHERE tenant=? AND principal=?",
        (tenant, principal),
    ).fetchall():
        capabilities = row["capabilities"]
        if isinstance(capabilities, str):
            capabilities = json.loads(capabilities)
        if "read" not in capabilities or not temporal_valid(at, row["starts"], row["expires"]):
            continue
        if row["project"] == "*":
            everything = True
        else:
            readable.add(row["project"])
    counts: dict[str, dict[str, int]] = {}

    def tally(statement: str, field: str) -> None:
        for row in db.execute(statement, (tenant,)).fetchall():
            project = row[0]
            if project is None or (not everything and project not in readable):
                continue
            counts.setdefault(project, {})[field] = int(row[1])

    tally(
        "SELECT project,count(*) FROM observations WHERE tenant=? AND deleted=0 GROUP BY project",
        "observations",
    )
    tally(
        "SELECT f.project,count(*) FROM revisions r JOIN families f ON r.tenant=f.tenant AND r.family=f.id WHERE r.tenant=? AND r.purged=0 GROUP BY f.project",
        "claims",
    )
    tally(
        "SELECT project,count(*) FROM checkpoint_streams WHERE tenant=? GROUP BY project",
        "tasks",
    )
    tally(
        "SELECT project,count(*) FROM rendered_artifacts WHERE tenant=? AND state='retained' GROUP BY project",
        "receipts",
    )
    for project in readable:
        counts.setdefault(project, {})
    items = []
    for project in sorted(counts):
        identifier(project)
        entry = counts[project]
        items.append(
            {
                "project": project,
                "observations": entry.get("observations", 0),
                "claims": entry.get("claims", 0),
                "tasks": entry.get("tasks", 0),
                "receipts": entry.get("receipts", 0),
            }
        )
    return items
