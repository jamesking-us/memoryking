"""Pure structured-plan invariants and identical progress for every client/store."""

from __future__ import annotations

from collections import Counter
from typing import Any

from memoryking.contracts.canonical import canonical_hash
from memoryking.domain.local import LocalError

STATES = ("todo", "doing", "done", "blocked", "skipped", "unknown")


def validate(plan: dict[str, Any], previous: dict[str, Any] | None = None) -> None:
    phases = {p["id"] for p in plan["phases"]}
    steps = {s["id"]: s for s in plan["steps"]}
    if len(phases) != len(plan["phases"]) or len(steps) != len(plan["steps"]):
        raise LocalError("plan_duplicate_id")
    for step in steps.values():
        if step["phase"] not in phases or any(d not in steps for d in step["depends_on"]):
            raise LocalError("plan_reference_invalid")
        if step["status"] == "done" and not step["evidence"]:
            raise LocalError("verification_evidence_required")
        if step["status"] in {"blocked", "skipped"} and not step["notes"].strip():
            raise LocalError("plan_reason_required")
        if step["status"] == "skipped" and not step["decision"]:
            raise LocalError("waiver_decision_required")
        if step["status"] in {"doing", "done"} and any(
            steps[d]["status"] not in {"done", "skipped"} for d in step["depends_on"]
        ):
            raise LocalError("plan_dependency_unresolved")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise LocalError("plan_dependency_cycle")
        if key in visited:
            return
        visiting.add(key)
        for dep in steps[key]["depends_on"]:
            visit(dep)
        visiting.remove(key)
        visited.add(key)

    for key in steps:
        visit(key)
    if plan["status"] == "done" and (
        not steps or any(s["status"] not in {"done", "skipped"} for s in steps.values())
    ):
        raise LocalError("plan_unresolved_steps")
    if plan["status"] in {"done", "abandoned"} and (
        not plan["lessons"] or not plan["closure_note"].strip()
    ):
        raise LocalError("plan_closure_evidence_required")
    if previous is not None:
        old = {s["id"]: s for s in previous["steps"]}
        for key, step in steps.items():
            if step["status"] != "done" or key not in old:
                continue
            definition_changed = definition(plan, key) != definition(previous, key)
            if definition_changed and not (proof_refs(step) - proof_refs(old[key])):
                raise LocalError("plan_reverification_required")


def proof_refs(step: dict[str, Any]) -> set[tuple[str, str]]:
    return {(d["kind"], d["id"]) for d in step["evidence"]}


def definition(plan: dict[str, Any], key: str) -> str:
    """Bind proof to the goal, step definition and transitive prerequisite states."""
    steps = {s["id"]: s for s in plan["steps"]}
    closure: set[str] = set()
    pending = list(steps[key]["depends_on"])
    while pending:
        dep = pending.pop()
        if dep in closure:
            continue
        closure.add(dep)
        pending.extend(steps[dep]["depends_on"])
    own = {k: steps[key][k] for k in ("description", "phase")}
    own["depends_on"] = sorted(steps[key]["depends_on"])
    return canonical_hash(
        {
            "objective": plan["objective"],
            "step": own,
            "dependencies": [
                {
                    **{
                        k: steps[d][k] for k in ("id", "description", "phase", "status", "decision")
                    },
                    "depends_on": sorted(steps[d]["depends_on"]),
                    "evidence": [
                        {"kind": kind, "id": identifier}
                        for kind, identifier in sorted(proof_refs(steps[d]))
                    ],
                }
                for d in sorted(closure)
            ],
        }
    )


def counts(steps: list[dict[str, Any]]) -> dict[str, Any]:
    count = Counter(s["status"] for s in steps)
    total = len(steps)
    done, skipped = count["done"], count["skipped"]
    return {
        "total": total,
        **{s: count[s] for s in STATES},
        "completed": done,
        "resolved": done + skipped,
        "work_percent": None if not total else 100 * done // total,
        "resolved_percent": None if not total else 100 * (done + skipped) // total,
    }


def progress(plan: dict[str, Any]) -> dict[str, Any]:
    phases = []
    for phase in plan["phases"]:
        value = counts([s for s in plan["steps"] if s["phase"] == phase["id"]])
        state = (
            "empty"
            if not value["total"]
            else (
                "resolved"
                if value["resolved"] == value["total"]
                else "blocked"
                if value["blocked"]
                else "in_progress"
                if value["doing"] or value["done"]
                else "not_started"
            )
        )
        phases.append({**phase, "state": state, "progress": value})
    return {
        **counts(plan["steps"]),
        "phases": phases,
        "current_phase": next((p["id"] for p in phases if p["state"] != "resolved"), None),
    }
