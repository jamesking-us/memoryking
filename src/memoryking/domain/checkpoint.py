"""Pure working-state invariants; arbitrary source text never acquires authority."""

from __future__ import annotations

from typing import Any

from memoryking.domain.local import LocalError


def stream_name(run: str | None) -> str:
    return "task" if run is None else "run:" + run


def dependencies(body: dict[str, Any]) -> list[dict[str, str]]:
    deps = list(body["references"])
    if "plan" in body:
        from memoryking.domain.plans import validate

        validate(body["plan"])
        deps.extend(body["plan"]["lessons"])
        for step in body["plan"]["steps"]:
            deps.extend(step["evidence"])
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
