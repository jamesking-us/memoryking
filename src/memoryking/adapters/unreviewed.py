"""The labelled tier: unreviewed drafts in a briefing, only where an admin has said so.

A briefing normally carries constraints, accepted knowledge and raw observations. With the
per-project policy on, it also carries a few drafted proposals in a section of their own,
below accepted knowledge, marked `drafted_unreviewed` with how they were drafted, how old
they are and the sentences they cite. They stay proposals: they never appear among
constraints, never satisfy `freshness: attested`, never count as accepted knowledge in
health or anywhere else, and a draft that is decided (accepted, rejected, deprecated),
quarantined, purged or no longer verifiable leaves the next briefing at once.

Only proposals that say how they were made are eligible: repeated evidence, an agent's
proposal, or a model's consolidation, of kind `fact`, `decision`, `preference` or
`failed_approach`. Never an `instruction`, a constraint or a procedure, and never a draft
that is a proposed replacement of an accepted claim (that is a conflict for a reviewer).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from memoryking.adapters.knowledge_store import Knowledge
from memoryking.application.storage import upsert
from memoryking.domain.briefing import relevance, terms
from memoryking.domain.local import LocalError

if TYPE_CHECKING:
    from memoryking.application.store_core import StoreCore

KINDS = {"fact", "decision", "preference", "failed_approach"}
METHODS = {
    "model_consolidation_v1": "model consolidation",
    "repeated_evidence_v1": "repeated evidence",
    "agent_proposal_v1": "agent proposal",
}
SCAN = 257  # newest undecided proposals examined
LIMIT = 3  # drafts in one briefing
QUOTES = 3
QUOTE_CHARS = 240


class Unreviewed:
    def __init__(self, store: StoreCore):
        self.s, self.db = store, store.db
        self.k = Knowledge(store)

    # --- Policy ----------------------------------------------------------------------

    def enabled(self, tenant: str, project: str) -> bool:
        row = self.db.execute(
            "SELECT enabled FROM unreviewed_tier WHERE tenant=? AND project=?", (tenant, project)
        ).fetchone()
        return row is not None and bool(row["enabled"])

    def show(self, who: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT enabled,decided_by,decided FROM unreviewed_tier WHERE tenant=? AND project=?",
            (who["tenant"], args["project"]),
        ).fetchone()
        return {
            "enabled": row is not None and bool(row["enabled"]),
            "decided_by": row["decided_by"] if row else None,
            "decided_ms": row["decided"] if row else None,
        }

    def set(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        enabled = args["mode"] == "on"
        self.s._apply(
            self.s._journal(
                {
                    "kind": "unreviewed_tier",
                    "tenant": who["tenant"],
                    "actor": who["principal"],
                    "project": args["project"],
                    "enabled": enabled,
                }
            ),
            at,
        )
        return {"project": args["project"], "enabled": enabled}

    def apply_policy(self, action: dict[str, Any], at: int) -> None:
        if not isinstance(action.get("enabled"), bool):
            raise LocalError("journal_invalid")
        self.db.execute(
            upsert("unreviewed_tier"),
            (
                action["tenant"],
                action["project"],
                1 if action["enabled"] else 0,
                action["actor"],
                at,
            ),
        )

    # --- Briefing --------------------------------------------------------------------

    def groups(
        self, who: dict[str, Any], args: dict[str, Any], at: int, recorded: int
    ) -> list[list[dict[str, Any]]]:
        """Drafted claims relevant to the query, as one-item groups for the packer. Empty when
        the policy is off. Every one is re-verified now: evidence spans intact, evidence
        readable by this reader, applicable under the session's bindings."""
        tenant, project = who["tenant"], args["project"]
        if not self.enabled(tenant, project):
            return []
        wanted = terms(args["query"])
        if not wanted:
            return []
        from memoryking.domain import selectors

        points = selectors.bindings(args["bindings"])
        rows = self.db.execute(
            "SELECT r.*,f.project,f.version AS family_version FROM revisions r JOIN families f ON r.tenant=f.tenant AND r.family=f.id WHERE r.tenant=? AND f.project=? AND r.kind='knowledge' AND r.purged=0 AND NOT EXISTS (SELECT 1 FROM lifecycle l WHERE l.tenant=r.tenant AND l.revision=r.id AND l.action IN ('accept','reject','deprecate')) AND NOT EXISTS (SELECT 1 FROM lifecycle a JOIN revisions o ON o.tenant=a.tenant AND o.id=a.revision WHERE a.tenant=r.tenant AND o.family=r.family AND a.action='accept') ORDER BY r.created DESC LIMIT ?",
            (tenant, project, SCAN),
        ).fetchall()
        found: list[tuple[int, int, dict[str, Any]]] = []
        for row in rows:
            body = json.loads(row["body"])
            provenance = body.get("provenance") or {}
            method = provenance.get("method")
            if method not in METHODS or provenance.get("relation"):
                continue
            kind = provenance.get("claim_kind", "fact")
            if kind not in KINDS or body.get("task") or body.get("run"):
                continue
            _, quarantined = self.k._state(tenant, row["id"], recorded)
            if (
                quarantined
                or not body.get("executable", True)
                or not self.k._available(who, row, at)
            ):
                continue
            score = relevance(wanted, body["text"])
            if not score:
                continue
            valid = args.get("valid_ms", at)
            matches = [self.k._eligible(row, body, b, valid, recorded, who, at) for b in points]
            if "true" not in matches:
                continue
            created = self.db.execute(
                "SELECT at_ms FROM events WHERE tenant=? AND position=?", (tenant, row["created"])
            ).fetchone()
            claim = {
                "reference": row["id"],
                "authority": "drafted_unreviewed",
                "family": row["family"],
                "text": body["text"],
                "selector": body["selector"],
                "valid_start": body["valid_start"],
                "valid_end": body["valid_end"],
                "task": None,
                "run": None,
                "applicability": matches,
                "code_validation": [],
                "issuer_authority": None,
                "effect": None,
                "rule_key": None,
                "rule_value": None,
                "evidence": body["evidence"],
                "assurance": "unreviewed_draft",
                "attestations": [],
                "selection_reason": "drafted_unreviewed",
                "conflicts": [],
                "lexical_score_ppm": score,
                "draft": {
                    "method": method,
                    "method_label": METHODS[method],
                    "kind": kind,
                    "age_ms": max(0, at - created["at_ms"]) if created else None,
                    "quotes": [
                        {"observation": q["observation"], "text": q["text"][:QUOTE_CHARS]}
                        for q in self.k._excerpts(
                            who, {**body, "evidence": body["evidence"][:QUOTES]}
                        )
                    ],
                },
            }
            found.append((-score, -row["created"], claim))
        found.sort(key=lambda item: (item[0], item[1], item[2]["reference"]))
        return [[claim] for _, _, claim in found[:LIMIT]]
