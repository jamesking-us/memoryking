"""The query-free briefing for a session's start.

A normal briefing needs a query; at session start there is none. This briefing answers
"what should I know before I begin" from four places, in this order and within the token
budget: the mandatory constraints for the session's bindings (resolved exactly as any
briefing resolves them, essential and indivisible); the project's pins (accepted claims
and observations an admin or reviewer chose to keep in front of every session); the
latest checkpoint the agent saved for the named task (objective, next actions, what not
to repeat); and the most recent decisions (claims accepted in the last N days, and the
newest observations whose source says they are decisions or instructions from the user).

Every item passes the same authorization, hold and corrections rules as a query briefing,
and the result carries the same receipt, so replay and purge follow the same dependencies.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from memoryking.adapters.corrections import Corrections
from memoryking.adapters.token_count import counter
from memoryking.domain.briefing import budget_scope, pack
from memoryking.domain.local import Decision, LocalError
from memoryking.domain.selectors import bindings

if TYPE_CHECKING:
    from memoryking.adapters.retrieval import Retrieval

# A source names a decision or an instruction from the user when it is `decision`,
# `instruction` or `user`, alone or followed by `-` or `_` and anything else
# (`user-instruction`, `decision_log`). Hooks and tools choose the source; nothing is
# inferred from the text.
DECISION_SOURCE = re.compile(r"^(decision|instruction|user)([-_].*)?$")
RECENT_DAYS = 14
RECENT_LIMIT = 5
EXCERPT_BYTES = 800
DAY_MS = 86_400_000
MAX_INT = 9_007_199_254_740_991


def _episode(retrieval: Retrieval, row: Any, reason: str) -> dict[str, Any]:
    raw = retrieval._blob(row)
    excerpt = raw[:EXCERPT_BYTES].decode("utf-8", errors="ignore")
    return {
        "reference": row["id"],
        "authority": "observed_episode",
        "text": excerpt,
        "actor": row["principal"],
        "source": row["source"],
        "observed_at_ms": row["observed_at_ms"],
        "assurance": "actor_reported",
        "selection_reason": reason,
        "excerpt_start_byte": 0,
        "excerpt_end_byte": len(excerpt.encode()),
        "source_bytes": len(raw),
        "content_hash": row["blob"],
    }


def _checkpoint_item(row: Any) -> dict[str, Any]:
    body = json.loads(row["body"])
    lines = [f"Objective: {body.get('objective', '')}"]
    if body.get("next_actions"):
        lines.append(
            "Next actions: " + " ".join(f"({n}) {a}" for n, a in enumerate(body["next_actions"], 1))
        )
    repeats = [
        f"{a.get('action', '')}: {a.get('outcome', '')} (not again when {a.get('do_not_repeat_when', 'conditions are unchanged')})"
        for a in body.get("failed_attempts", [])
    ]
    if repeats:
        lines.append("Do not repeat: " + "; ".join(repeats))
    return {
        "reference": row["id"],
        "authority": "agent_checkpoint",
        "text": "\n".join(lines),
        "objective": body.get("objective", ""),
        "next_actions": body.get("next_actions", []),
        "do_not_repeat": repeats,
        "writer": row["writer"],
        "revision": row["revision"],
        "created_ms": row["created"],
        "assurance": "actor_reported",
        "selection_reason": "last_checkpoint",
    }


def start(
    retrieval: Retrieval,
    who: dict[str, Any],
    args: dict[str, Any],
    decision: Decision,
    receipt: str,
) -> tuple[bytes, dict[str, Any], list[dict[str, str]]]:
    s, db, k = retrieval.s, retrieval.db, retrieval.k
    at, tenant, project = decision.evaluated_ms, who["tenant"], args["project"]
    started = s.clock.monotonic_ms()
    bindings(args["bindings"])
    count, usage = counter(args["tokenizer"])
    days = args.get("recent_days", RECENT_DAYS)
    limit = args.get("recent_limit", RECENT_LIMIT)
    source = db.execute("SELECT position FROM tenants WHERE tenant=?", (tenant,)).fetchone()[0]
    scope: dict[str, Any] = {
        k_: args[k_] for k_ in ("project", "bindings", "task", "run") if k_ in args
    }
    mandatory = k.query(who, scope, at, constraints=True)
    base = retrieval._base(args, decision, receipt, usage)
    base.update(
        objective="session start",
        mode="start",
        coverage={
            "lexical": "not_used",
            "dense": "not_used",
            "optional": "query_free_start",
            "required_missing": [],
        },
        snapshot={"source_position": source, "recorded_position": source, "valid_ms": at},
    )
    groups: list[list[dict[str, Any]]] = []
    checkpoint = None
    if mandatory["status"] != "complete":
        base.update(status="blocked", reason=mandatory["reason"])
        if mandatory.get("unbound_fields"):
            base["unbound_fields"] = mandatory["unbound_fields"]
        if mandatory["reason"] == "unresolved_conflict":
            base["constraints"] = [retrieval._claim(i) for i in mandatory["items"]]
            base["conflicts"] = mandatory.get("conflicts", [])
    else:
        base["constraints"] = [retrieval._claim(i) for i in mandatory["items"]]
        knowledge = k.query(who, scope, at, constraints=False)
        accepted = {
            i["revision"]: i for i in knowledge["items"] if i["body"]["kind"] == "knowledge"
        }
        by_family = {i["family"]: i for i in accepted.values()}
        seen: set[str] = set()
        corrections = Corrections(s)

        def observation(ident: str) -> Any:
            row = db.execute(
                "SELECT * FROM observations WHERE tenant=? AND id=? AND project=?",
                (tenant, ident, project),
            ).fetchone()
            if row is None or not s._visible(who, row, at):
                return None
            hidden, marked = corrections.screen(who, [ident], source, at)
            return None if ident in hidden else (row, ident in marked)

        # 1. Pins, in the order they were made.
        for pin in corrections.pinned(tenant, project):
            if pin["kind"] == "claim":
                item = by_family.get(pin["target"])
                if item is not None and item["revision"] not in seen:
                    seen.add(item["revision"])
                    claim = retrieval._claim(item)
                    claim["selection_reason"] = "pinned"
                    groups.append([claim])
            else:
                found = observation(pin["target"])
                if found is not None and pin["target"] not in seen:
                    seen.add(pin["target"])
                    entry = _episode(retrieval, found[0], "pinned")
                    entry["retirement_requested"] = found[1] or None
                    groups.append([{k_: v for k_, v in entry.items() if v is not None}])
        # 2. The agent's latest checkpoint for the named task.
        if args.get("task"):
            from memoryking.adapters.working import Working

            try:
                checkpoint = Working(s)._checkpoint(
                    who,
                    {
                        "project": project,
                        "task": args["task"],
                        "run": args.get("run"),
                        "checkpoint": None,
                    },
                    at,
                )
            except LocalError:
                checkpoint = None
            if checkpoint is not None:
                groups.append([_checkpoint_item(checkpoint)])
        # 3. Recent decisions, newest first: accepted claims, and observations whose
        # source marks them as the user's decisions or instructions.
        recent: list[tuple[int, dict[str, Any]]] = []
        since = at - days * DAY_MS
        for row in db.execute(
            "SELECT l.revision,l.position FROM lifecycle l JOIN events ev ON ev.tenant=l.tenant AND ev.position=l.position WHERE l.tenant=? AND l.action='accept' AND ev.at_ms>=? ORDER BY l.position DESC LIMIT 257",
            (tenant, since),
        ).fetchall():
            item = accepted.get(row["revision"])
            if item is None or row["revision"] in seen:
                continue
            if len([1 for _, e in recent if e["authority"] != "observed_episode"]) >= limit:
                break
            seen.add(row["revision"])
            claim = retrieval._claim(item)
            claim["selection_reason"] = "recent_decision"
            recent.append((row["position"], claim))
        taken = 0
        for row in db.execute(
            retrieval.SCOPE
            + "(o.source LIKE ? OR o.source LIKE ? OR o.source LIKE ?) ORDER BY o.position DESC,o.id DESC LIMIT 257",
            (tenant, project, source, who["principal"], "user%", "decision%", "instruction%"),
        ).fetchall():
            if taken >= limit:
                break
            found = observation(row["id"])
            if found is None or row["id"] in seen or not DECISION_SOURCE.match(found[0]["source"]):
                continue
            seen.add(row["id"])
            taken += 1
            entry = _episode(retrieval, found[0], "user_decision")
            if found[1]:
                entry["retirement_requested"] = True
            recent.append((row["position"], entry))
        groups.extend([item] for _, item in sorted(recent, key=lambda pair: -pair[0]))
        nothing = not base["constraints"] and not groups
        base["status"] = "no_relevant_memory" if nothing else "partial"
        if nothing:
            base["coverage"]["optional"] = "nothing_recorded"
        if knowledge["status"] != "complete":
            base["warnings"].append("optional_eligibility_incomplete")
        if knowledge.get("conflicts"):
            base["warnings"].append("optional_conflict_possible")
    raw, used, selected, omitted = pack(
        base, groups, count, args["max_output_tokens"], retrieval.renderer(args)
    )
    output = json.loads(raw)
    rendered = output["constraints"] + [i for i in selected if i["authority"] != "agent_checkpoint"]
    deps = retrieval._dependencies(rendered)
    if checkpoint is not None and any(i["authority"] == "agent_checkpoint" for i in selected):
        deps.append({"kind": "checkpoint", "id": checkpoint["id"]})
        deps.extend(json.loads(checkpoint["dependencies"]))
    diag: dict[str, Any] = {
        "mode": "start",
        "store": s.store_id,
        "incarnation": s.incarnation,
        "actor": who["principal"],
        "project": project,
        "source_position": source,
        "evaluated_ms": at,
        "deadline_ms": decision.deadline_ms,
        "recent_days": days,
        "recent_limit": limit,
        "status": output["status"],
        "minimum_required_tokens": output.get("minimum_required_tokens"),
        "budget_count_scope": budget_scope(output),
        "usage": {**output["usage"], "output_tokens": used, "serialized_bytes": len(raw)},
        "coverage": output["coverage"],
        "selected": [
            {
                "reference": i["reference"],
                "authority": i["authority"],
                "reason": i["selection_reason"],
            }
            for i in [*output["constraints"], *selected]
        ],
        "omissions": sorted(set(omitted)),
        "trace": [],
        "duration_ms": max(0, s.clock.monotonic_ms() - started),
    }
    return raw, diag, deps
