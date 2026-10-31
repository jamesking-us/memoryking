"""Pure working-state invariants; arbitrary source text never acquires authority."""

from __future__ import annotations

from typing import Any

from memoryking.domain.local import LocalError


def stream_name(run: str | None) -> str:
    return "task" if run is None else "run:" + run


def dependencies(body: dict[str, Any]) -> list[dict[str, str]]:
    deps = list(body["references"])
    for field in ("acceptance", "findings", "verification"):
        for item in body[field]:
            deps.extend(item["evidence"])
    for artifact in body["repository"]["artifacts"]:
        if artifact["availability"] == "stored":
            if artifact["observation"] is None or artifact["content_hash"] is None:
                raise LocalError("artifact_reference_required")
            deps.append({"kind": "observation", "id": artifact["observation"]})
        elif artifact["observation"] is not None or artifact["content_hash"] is not None:
            raise LocalError("artifact_availability_invalid")
    for item in body["acceptance"]:
        if item["state"] == "passed" and not item["evidence"]:
            raise LocalError("verification_evidence_required")
        if item["state"] == "waived" and item["decision"] is None:
            raise LocalError("waiver_decision_required")
    for item in body["verification"]:
        if item["result"] in {"passed", "failed"} and not item["evidence"]:
            raise LocalError("verification_evidence_required")
    if len({i["id"] for i in body["acceptance"]}) != len(body["acceptance"]):
        raise LocalError("duplicate_criterion")
    unique = {(d["kind"], d["id"]) for d in deps}
    if len(unique) > 128:
        raise LocalError("checkpoint_dependency_limit")
    return [{"kind": k, "id": i} for k, i in sorted(unique)]


def operation_transition(current: str, target: str, reconciled: bool) -> None:
    allowed = {
        "intent_recorded": {"dispatching"},
        "dispatching": {"unknown", "succeeded", "failed"},
        "unknown": {"succeeded", "failed"},
        "succeeded": {"succeeded"} if reconciled else set(),
        "failed": {"failed"} if reconciled else set(),
    }
    if target not in allowed[current]:
        raise LocalError("operation_transition_invalid")
    if current == "unknown" and not reconciled:
        raise LocalError("operation_reconciliation_required")
    if reconciled and target not in {"succeeded", "failed"}:
        raise LocalError("operation_transition_invalid")


MINIMAL_SECTIONS = ("objective", "current_state")
DEFAULT_SECTIONS: dict[str, Any] = {
    "acceptance": [],
    "hard_constraints": [],
    "findings": [],
    "failed_attempts": [],
    "verification": [],
    "blockers": [],
    "next_actions": [],
    "references": [],
    "operation_ids": [],
}


def complete(body: Any, observed_ms: int) -> dict[str, Any]:
    """Fill a partial checkpoint so it can be saved in one call.

    `objective` and `current_state` must be supplied. Every other section defaults to
    empty, the repository to "none recorded", and each defaulted section is listed in
    `capture.gaps` as `defaulted:<section>` so a resume states what was never written
    rather than presenting an empty list as a finding of nothing.
    """
    if not isinstance(body, dict) or any(
        not isinstance(body.get(field), str) or not body[field] for field in MINIMAL_SECTIONS
    ):
        raise LocalError("invalid_checkpoint")
    completed = dict(body)
    gaps: list[str] = []
    for section, default in DEFAULT_SECTIONS.items():
        if section not in completed:
            completed[section] = list(default)
            gaps.append("defaulted:" + section)
    if "repository" not in completed:
        completed["repository"] = {
            "repository": None,
            "base_commit": None,
            "current_commit": None,
            "dirty": False,
            "artifacts": [],
        }
        gaps.append("defaulted:repository")
    capture = completed.get("capture")
    if not isinstance(capture, dict):
        capture = {"last_event": None, "observed_ms": observed_ms, "gaps": []}
        gaps.append("defaulted:capture")
    else:
        capture = dict(capture)
        capture.setdefault("last_event", None)
        capture.setdefault("observed_ms", observed_ms)
        capture.setdefault("gaps", [])
    existing = capture["gaps"] if isinstance(capture["gaps"], list) else []
    capture["gaps"] = list(existing) + [g for g in gaps if g not in existing]
    completed["capture"] = capture
    return completed
