"""Source trust: captures from an untrusted source are held until an admin releases them.

A held observation exists, is backed up and can be purged, but it is invisible to every
read, briefing, graph, resume and claim proposal until released. Trust levels and
releases are policy: they go through the signed control journal, so a restore replays
them and the policy epoch retires views that were built before the decision.
"""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING, Any

from memoryking.application.storage import upsert
from memoryking.domain.local import LocalError

if TYPE_CHECKING:
    from memoryking.application.store_core import StoreCore

LEVELS = ("trusted", "untrusted")
PREVIEW_BYTES = 256


class Trust:
    def __init__(self, store: StoreCore):
        self.s, self.db = store, store.db

    def rules(self, tenant: str, project: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT project,source,level,decided_by,decided FROM source_trust WHERE tenant=? AND project IN (?,'*') ORDER BY project DESC,source",
            (tenant, project),
        ).fetchall()
        return [
            dict(zip(("project", "source", "level", "decided_by", "decided_ms"), r, strict=True))
            for r in rows
        ]

    def level(self, tenant: str, project: str, source: str) -> str:
        """The most specific rule wins: exact source over pattern, project over `*`."""
        best: tuple[int, str] | None = None
        for rule in self.rules(tenant, project):
            if rule["source"] == source:
                score = 4
            elif fnmatch.fnmatchcase(source, rule["source"]):
                score = 2
            else:
                continue
            if rule["project"] == project:
                score += 1
            if best is None or score > best[0]:
                best = (score, rule["level"])
        return best[1] if best else "trusted"

    def hold(self, tenant: str, project: str, observation: str, source: str, at: int) -> None:
        self.db.execute(
            "INSERT INTO observation_holds(tenant,id,project,reason,held,state) VALUES (?,?,?,?,?,'held')",
            (tenant, observation, project, f"untrusted_source:{source}", at),
        )

    def held(self, tenant: str, observation: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM observation_holds WHERE tenant=? AND id=? AND state='held'",
                (tenant, observation),
            ).fetchone()
            is not None
        )

    def listing(self, who: dict[str, Any], project: str, limit: int = 64) -> list[dict[str, Any]]:
        """Held observations an admin may review, oldest first, with a bounded preview."""
        items = []
        for row in self.db.execute(
            "SELECT h.id,h.reason,h.held,o.tenant,o.source,o.principal,o.position,o.blob FROM observation_holds h JOIN observations o ON o.tenant=h.tenant AND o.id=h.id WHERE h.tenant=? AND h.project=? AND h.state='held' AND o.deleted=0 ORDER BY h.held,h.id LIMIT ?",
            (who["tenant"], project, limit + 1),
        ).fetchall()[:limit]:
            from memoryking.adapters.retrieval import Retrieval

            text = Retrieval(self.s)._blob(row) if row["blob"] else b""
            preview = text[:PREVIEW_BYTES].decode("utf-8", "ignore")
            items.append(
                {
                    "observation": row["id"],
                    "source": row["source"],
                    "captured_by": row["principal"],
                    "position": row["position"],
                    "reason": row["reason"],
                    "held_ms": row["held"],
                    "preview": preview + ("…" if len(text) > PREVIEW_BYTES else ""),
                }
            )
        return items

    def apply_policy(self, action: dict[str, Any], at: int) -> None:
        tenant = action["tenant"]
        if action["kind"] == "trust":
            if action["level"] not in LEVELS:
                raise LocalError("trust_level_invalid")
            if action["level"] == "trusted" and not action.get("explicit", True):
                self.db.execute(
                    "DELETE FROM source_trust WHERE tenant=? AND project=? AND source=?",
                    (tenant, action["project"], action["source"]),
                )
                return
            self.db.execute(
                upsert("source_trust"),
                (
                    tenant,
                    action["project"],
                    action["source"],
                    action["level"],
                    action["actor"],
                    at,
                ),
            )
        elif action["kind"] == "hold":
            changed = self.db.execute(
                "UPDATE observation_holds SET state='released',released_by=?,released=? WHERE tenant=? AND id=? AND state='held'",
                (action["actor"], at, tenant, action["observation"]),
            ).rowcount
            if not changed:
                raise LocalError("hold_unavailable")
        else:
            raise LocalError("journal_invalid")
