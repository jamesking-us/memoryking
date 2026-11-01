"""Current-authorized task observations; progress comes from checkpoints, never event counts."""

from __future__ import annotations

import json
from typing import Any

from memoryking.adapters.working import Working
from memoryking.application.store_core import StoreCore
from memoryking.contracts.canonical import (
    ContractError,
    canonical_bytes,
    canonical_hash,
    load_strict,
)
from memoryking.domain.local import LocalError

EVENT_KINDS = frozenset(
    {
        "task_started",
        "task_started_recovered",
        "recovered_staged_state",
        "periodic_state",
        "before_action_state",
        "accepted_constraint",
        "structured_state_changed",
        "checkpoint",
        "tool_started",
        "tool_not_dispatched",
        "tool_readback",
        "tool_completed",
        "tool_returned_error",
        "model_started",
        "model_completed",
        "model_failed",
        "model_interrupted",
        "artifact_stored",
        "shutdown",
    }
)


def scope_hash(criteria: list[dict[str, Any]]) -> str:
    return canonical_hash(
        sorted(
            ({"id": c["id"], "text": c["text"], "weight": c.get("weight", 1)} for c in criteria),
            key=lambda c: c["id"],
        )
    )


class Execution:
    def __init__(self, store: StoreCore):
        self.s, self.db = store, store.db
        self.w = Working(store)

    def _evidence(self, who: dict[str, Any], dep: dict[str, str], at: int) -> bool:
        try:
            self.w._dependency(who, dep, at)
            if dep["kind"] == "observation":
                row = self.db.execute(
                    "SELECT * FROM observations WHERE tenant=? AND id=?", (who["tenant"], dep["id"])
                ).fetchone()
                self.w.r._blob(row)
            else:
                row = self.w.r.k._revision(who["tenant"], dep["id"])
                for ref in self.w.r.k._evidence(who["tenant"], row["id"]):
                    observation = self.db.execute(
                        "SELECT * FROM observations WHERE tenant=? AND id=?",
                        (who["tenant"], ref["observation"]),
                    ).fetchone()
                    self.w.r._blob(observation)
            return True
        except (LocalError, OSError):
            return False

    def snapshot(
        self, who: dict[str, Any], project: str, at: int, recorded_position: int | None = None
    ) -> dict[str, Any]:
        current_position = self.db.execute(
            "SELECT position FROM tenants WHERE tenant=?", (who["tenant"],)
        ).fetchone()[0]
        frontier = current_position if recorded_position is None else recorded_position
        if type(frontier) is not int or not 0 <= frontier <= current_position:
            raise LocalError("invalid_recorded_position")
        result: dict[str, Any] = {
            "schema_version": 1,
            "project": project,
            "store": self.s.store_id,
            "tenant": who["tenant"],
            "principal": who["principal"],
            "incarnation": self.s.incarnation,
            "observed_ms": at,
            "fresh_until_ms": at + 15000,
            "status": "available",
            "tasks": [],
            "events": [],
            "coverage": "up_to_8_visible_tasks_from_64_recent_streams_and_32_events_from_128_candidates",
            "authority": "authenticated_producer_reports_with_current_evidence_access; not_independent_test_certification",
            "mode": "live" if recorded_position is None else "historical",
            "recorded_position": frontier,
            "authorization_evaluated_ms": at,
            "external_actions": 0,
        }
        streams = self.db.execute(
            "SELECT c.task,c.stream,c.writer,c.revision,c.id AS latest,c.created FROM checkpoints c WHERE c.tenant=? AND c.project=? AND c.incarnation=? AND c.position<=? AND NOT EXISTS(SELECT 1 FROM checkpoints newer WHERE newer.tenant=c.tenant AND newer.project=c.project AND newer.task=c.task AND newer.stream=c.stream AND newer.incarnation=c.incarnation AND newer.position<=? AND newer.revision>c.revision) ORDER BY c.created DESC,c.task,c.stream LIMIT 64",
            (who["tenant"], project, self.s.incarnation, frontier, frontier),
        ).fetchall()
        task_index: dict[tuple[str, str], dict[str, Any]] = {}
        linked_events: dict[tuple[str, str], set[str]] = {}
        for stream in streams:
            if recorded_position is None:
                current_stream = self.db.execute(
                    "SELECT writer,revision FROM checkpoint_streams WHERE tenant=? AND project=? AND task=? AND stream=?",
                    (who["tenant"], project, stream["task"], stream["stream"]),
                ).fetchone()
                if current_stream is not None:
                    stream = {**dict(stream), **dict(current_stream)}
            row = self.db.execute(
                "SELECT * FROM checkpoints WHERE tenant=? AND id=?",
                (who["tenant"], stream["latest"]),
            ).fetchone()
            if row["state"] != "available" or who["principal"] not in json.loads(row["readers"]):
                continue
            args = {
                "project": project,
                "task": stream["task"],
                "run": None if stream["stream"] == "task" else stream["stream"][4:],
                "checkpoint": row["id"],
            }
            try:
                self.w._checkpoint(who, args, at)
            except LocalError:
                # Do not release a copied title/criterion after a source restriction.
                return {
                    **result,
                    "status": "unavailable",
                    "tasks": [],
                    "events": [],
                    "reason": "current_source_authorization_unavailable",
                }
            body = json.loads(row["body"])
            history = self.db.execute(
                "SELECT id FROM checkpoints WHERE tenant=? AND project=? AND task=? AND stream=? AND incarnation=? AND revision<? ORDER BY revision DESC LIMIT 129",
                (
                    who["tenant"],
                    project,
                    stream["task"],
                    stream["stream"],
                    self.s.incarnation,
                    row["revision"],
                ),
            ).fetchall()
            # Never let an audit-only checkpoint rehabilitate a changed definition
            # carrying unchanged proof. Bounded or inaccessible history stays unknown.
            history_complete = len(history) <= 128
            previous_body = None
            prior_bodies = []
            for index, previous in enumerate(history[:128]):
                try:
                    prior = json.loads(
                        self.w._checkpoint(who, {**args, "checkpoint": previous["id"]}, at)["body"]
                    )
                    prior_bodies.append(prior)
                    if index == 0:
                        previous_body = prior
                except LocalError:
                    history_complete = False
            old = {} if previous_body is None else {c["id"]: c for c in previous_body["acceptance"]}
            counts = {state: 0 for state in ("passed", "failed", "pending", "waived", "unknown")}
            weighted = dict.fromkeys(counts, 0)
            criteria = []
            for criterion in body["acceptance"]:
                available = bool(criterion["evidence"]) and all(
                    self._evidence(who, dep, at) for dep in criterion["evidence"]
                )
                effective = criterion["state"]
                predecessor = old.get(criterion["id"])
                changed = predecessor is not None and predecessor["text"] != criterion["text"]
                # A changed definition carrying the same old proof is not new verification.
                proof = sorted(criterion["evidence"], key=lambda d: (d["kind"], d["id"]))
                needs_reverification = not history_complete or any(
                    c["id"] == criterion["id"]
                    and c["text"] != criterion["text"]
                    and sorted(c["evidence"], key=lambda d: (d["kind"], d["id"])) == proof
                    for prior in prior_bodies
                    for c in prior["acceptance"]
                )
                if effective == "passed" and (not available or needs_reverification):
                    effective = "unknown"
                counts[effective] += 1
                weighted[effective] += criterion.get("weight", 1)
                criteria.append(
                    {
                        "id": criterion["id"],
                        "text": criterion["text"][:360],
                        "text_clipped": len(criterion["text"]) > 360,
                        "reported": criterion["state"],
                        "weight": criterion.get("weight", 1),
                        "effective": effective,
                        "evidence_available": available,
                        "evidence": criterion["evidence"],
                        "decision": criterion["decision"],
                        "definition_changed": changed,
                        "reverification_required": needs_reverification,
                        "reopened": predecessor is not None
                        and predecessor["state"] == "passed"
                        and effective != "passed",
                    }
                )
            current_scope = scope_hash(body["acceptance"])
            try:
                operations = (
                    []
                    if recorded_position is not None
                    else self.w._operations(who, args, at, include_all=True)[0]
                )
            except LocalError:
                return {
                    **result,
                    "status": "unavailable",
                    "tasks": [],
                    "events": [],
                    "reason": "current_source_authorization_unavailable",
                }
            task = {
                "task": stream["task"],
                "run": args["run"],
                "checkpoint": row["id"],
                "revision": row["revision"],
                "stream_revision": stream["revision"],
                "writer": stream["writer"],
                "checkpoint_writer": row["writer"],
                "body_hash": row["body_hash"],
                "source_position": row["position"],
                "created_ms": row["created"],
                "objective": body["objective"][:360],
                "summary": self.w.latest_summary(who, row["id"], at),
                "current_state": body["current_state"][:600],
                "display_may_be_clipped": True,
                "criteria": criteria,
                "progress": {
                    **counts,
                    "total": len(criteria),
                    "weighted": {
                        **weighted,
                        "total": sum(weighted.values()),
                        "basis": "declared_weight_default_one",
                    },
                    "scope_hash": current_scope,
                    "history_complete": history_complete,
                    "previous_total": None
                    if previous_body is None
                    else len(previous_body["acceptance"]),
                    "scope_changed": None
                    if previous_body is None
                    else current_scope != scope_hash(previous_body["acceptance"]),
                    "reopened": sum(c["reopened"] for c in criteria),
                },
                "blockers": [
                    {"text": b["text"][:360], "owner": b["owner"]} for b in body["blockers"]
                ],
                "verification": [
                    {
                        "name": v["name"][:240],
                        "result": v["result"],
                        "target_revision": v["target_revision"],
                        "environment": v["environment"][:120],
                    }
                    for v in body["verification"]
                ],
                "operations": [
                    {
                        "id": op["id"],
                        "state": op["state"],
                        "reconciliation_required": op["reconciliation_required"],
                    }
                    for op in operations
                ],
                "capture_gaps": body["capture"]["gaps"],
                "heartbeat": {"status": "unknown", "recorded_ms": None},
                "repository_freshness": "not_evaluated_by_task_projection",
                "operations_coverage": "current"
                if recorded_position is None
                else "historical_operation_versions_not_retained",
            }
            result["tasks"].append(task)
            task_index[(stream["task"], stream["stream"])] = task
            linked_events[(stream["task"], stream["stream"])] = {
                dep["id"] for dep in json.loads(row["dependencies"]) if dep["kind"] == "observation"
            }
            if len(result["tasks"]) >= 8:
                break
        candidates = self.db.execute(
            "SELECT o.*,e.at_ms AS recorded_ms FROM observations o JOIN events e ON e.tenant=o.tenant AND e.position=o.position WHERE o.tenant=? AND o.project=? AND o.position<=? AND o.source IN ('reference-harness-v1','reference-heartbeat-v1') ORDER BY o.position DESC LIMIT 128",
            (who["tenant"], project, frontier),
        ).fetchall()
        for row in candidates:
            if not self.s._visible(who, row, at):
                continue
            try:
                event = load_strict(self.w.r._blob(row))
            except (LocalError, OSError, ContractError):
                continue
            if (
                not isinstance(event, dict)
                or event.get("version") != 1
                or event.get("id") != row["source_event"]
                or event.get("project") != project
            ):
                continue
            if not isinstance(event.get("task"), str) or (
                event.get("run") is not None and not isinstance(event.get("run"), str)
            ):
                continue
            stream_key = (
                event["task"],
                "task" if event.get("run") is None else "run:" + event["run"],
            )
            matched_task = task_index.get(stream_key)
            if matched_task is None:
                continue
            if row["source"] == "reference-heartbeat-v1":
                if recorded_position is not None:
                    continue  # A historical contact is not current liveness.
                if (
                    matched_task["heartbeat"]["status"] != "unknown"
                    or event.get("checkpoint") != matched_task["checkpoint"]
                    or event.get("revision") != matched_task["revision"]
                    or row["principal"] != matched_task["writer"]
                ):
                    continue
                matched_task["heartbeat"] = {
                    "status": "recent" if 0 <= at - row["recorded_ms"] <= 120000 else "stale",
                    "recorded_ms": row["recorded_ms"],
                    "activity": event.get("activity")
                    if isinstance(event.get("activity"), str)
                    and event.get("activity") in {"idle", "model", "tool"}
                    else "unknown",
                }
            elif (
                row["id"] in linked_events[stream_key]
                and len(result["events"]) < 32
                and isinstance(event.get("kind"), str)
                and event.get("kind") in EVENT_KINDS
            ):
                result["events"].append(
                    {
                        "id": row["id"],
                        "event_id": row["source_event"],
                        "kind": event["kind"],
                        "task": event["task"],
                        "run": event.get("run"),
                        "actor": row["principal"],
                        "recorded_ms": row["recorded_ms"],
                        "observed_ms": row["observed_at_ms"],
                        "source_position": row["position"],
                    }
                )
        if len(canonical_bytes(result)) > 262144:
            return {
                **result,
                "status": "unavailable",
                "tasks": [],
                "events": [],
                "reason": "snapshot_limit",
            }
        return result
