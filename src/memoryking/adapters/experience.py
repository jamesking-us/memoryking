"""Reviewed procedures under the shared transaction, evidence and release boundary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from memoryking.adapters.knowledge_store import Knowledge
from memoryking.adapters.local_store import RETRY_MS, LocalStore, uid
from memoryking.adapters.repository_store import Repository
from memoryking.contracts.canonical import canonical_bytes, canonical_hash
from memoryking.domain import selectors
from memoryking.domain.local import LocalError, Released, temporal_valid

VALIDATOR = Draft202012Validator(
    json.loads(Path(__file__).with_name("experience.schema.json").read_text())
)
POLICY = "reviewed-procedure-v1"
LIMIT = 256


class Experience:
    def __init__(self, store: LocalStore):
        self.s, self.db, self.k = store, store.db, Knowledge(store)

    def revision(self, who: dict[str, Any], args: dict[str, Any], at: int) -> Any:
        row = self.k._revision(who["tenant"], args["revision"], args["project"])
        if row["kind"] != "procedure" or not self.k._available(who, row, at):
            raise LocalError("procedure_unavailable")
        return row

    def execute(self, credential: str, command: dict[str, Any]) -> Released:
        if len(canonical_bytes(command)) > 131072 or not VALIDATOR.is_valid(command):
            raise LocalError("invalid_request")
        if command["incarnation"] != self.s.incarnation:
            raise LocalError("history_changed")
        action, args, receipt = command["action"], command["args"], uid("rcpt")
        mutation = action not in {"query", "records", "record", "ranking_inspect"}
        with self.s.transaction() as decision:
            at = decision.evaluated_ms
            who = self.s._auth(credential, "read", args["project"], at)
            cap = "capture" if action in {"propose", "outcome", "feedback"} else "review"
            if mutation or args.get("diagnostic", False):
                self.s._auth(credential, cap, args["project"], at)
            if action.startswith("ranking_"):
                self.s._auth(credential, "review", args["project"], at)
                self.s._auth(credential, "admin", args["project"], at)
            if action == "propose":
                self.proposal(who, args, at)
            elif action in {"review", "activate", "suspend"}:
                self.revision(who, args, at)
            lookup = (
                who["tenant"],
                self.s.incarnation,
                "experience-v1",
                who["principal"],
                action,
                command.get("key"),
            )
            previous = (
                self.db.execute(
                    "SELECT * FROM retries WHERE tenant=? AND incarnation=? AND namespace=? AND principal=? AND operation=? AND key=?",
                    lookup,
                ).fetchone()
                if mutation
                else None
            )
            if previous is not None:
                if previous["hash"] != canonical_hash(command):
                    raise LocalError("idempotency_conflict")
                if previous["result"] is None or at >= previous["expires"]:
                    raise LocalError("idempotency_result_expired")
                result = json.loads(previous["result"])
                result["replayed"] = True
            elif action.startswith("ranking_"):
                from memoryking.adapters.ranking import Ranking

                ranking = Ranking(self.s)
                if action == "ranking_propose":
                    result = ranking.propose(who, args, at)
                elif action == "ranking_evaluate":
                    result = ranking.evaluate(who, args, decision)
                elif action == "ranking_inspect":
                    result = ranking.inspect(who, args, at)
                else:
                    result = ranking.transition(who, args, action.removeprefix("ranking_"), at)
            elif action in {"outcome", "feedback", "records", "record"}:
                from memoryking.adapters.experience_records import ExperienceRecords

                result = getattr(ExperienceRecords(self.s), action)(who, args, at)
            elif action == "propose":
                result = self.k.propose(credential, who, args, at)
            elif action == "review":
                result = self.k.review(credential, who, args, at, procedure=True)
            elif action in {"activate", "suspend"}:
                result = self.transition(who, args, action, at)
            else:
                result = self.query(who, args, at)
            if mutation and previous is None:
                self.db.execute(
                    "INSERT INTO retries VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (*lookup, canonical_hash(command), json.dumps(result), None, at + RETRY_MS),
                )
            result.update(
                incarnation=self.s.incarnation,
                evaluated_ms=at,
                deadline_ms=decision.deadline_ms,
                receipt=receipt,
            )
            raw = canonical_bytes(result)
            if len(raw) > 262144:
                raise LocalError("experience_response_limit")
            epochs = self.db.execute(
                "SELECT policy_epoch,purge_epoch FROM tenants WHERE tenant=?", (who["tenant"],)
            ).fetchone()
            self.db.execute(
                "INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    who["tenant"],
                    receipt,
                    who["principal"],
                    self.s.incarnation,
                    at,
                    decision.deadline_ms,
                    *epochs,
                    canonical_hash(result),
                    "[]",
                ),
            )
            self.s.fault("after_validation")
        self.s._expose()
        return Released(raw, decision, self.s.clock, receipt)

    def proposal(self, who: dict[str, Any], args: dict[str, Any], at: int) -> None:
        body = args["body"]
        procedure = body["procedure"]
        evidence = {span["observation"] for span in body["evidence"]}
        if not set(procedure["success_evidence"] + procedure["failure_evidence"]) <= evidence:
            raise LocalError("procedure_support_required")
        if set(procedure["success_evidence"]) & set(procedure["failure_evidence"]):
            raise LocalError("procedure_case_ambiguous")
        self.k._support(who, body["evidence"], at)
        selectors.canonical(body["selector"])
        # A knowledge family cannot turn into a procedure/constraint under a new name.
        previous = self.db.execute(
            "SELECT r.kind FROM revisions r JOIN families f ON r.tenant=f.tenant AND r.family=f.id WHERE r.tenant=? AND f.project=? AND f.logical_key=? LIMIT 1",
            (who["tenant"], args["project"], args["logical_key"]),
        ).fetchone()
        if previous is not None and previous[0] != "procedure":
            raise LocalError("family_kind_mismatch")

    def activation(self, tenant: str, revision: str, position: int) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT action,detail,position FROM lifecycle WHERE tenant=? AND revision=? AND action IN ('activate','suspend') AND position<=? ORDER BY position DESC LIMIT 1",
            (tenant, revision, position),
        ).fetchone()
        if row is None:
            return None
        return {"action": row["action"], "position": row["position"], **json.loads(row["detail"])}

    def matches(
        self, who: dict[str, Any], row: Any, points: list[dict[str, Any]], at: int, position: int
    ) -> list[str]:
        body = json.loads(row["body"])
        if (
            body["procedure"]["dependency_mode"] == "required"
            and self.db.execute(
                "SELECT 1 FROM code_policies WHERE tenant=? AND revision=?",
                (who["tenant"], row["id"]),
            ).fetchone()
            is None
        ):
            return ["unknown"] * len(points)
        return [self.k._eligible(row, body, point, at, position, who, at) for point in points]

    def transition(
        self, who: dict[str, Any], args: dict[str, Any], action: str, at: int
    ) -> dict[str, Any]:
        if (
            action == "activate"
            and self.db.execute(
                "SELECT recovery_gap FROM knowledge_state WHERE tenant=?", (who["tenant"],)
            ).fetchone()[0]
        ):
            raise LocalError("knowledge_reconciliation_required")
        row = self.revision(who, args, at)
        if row["family_version"] != args["expected_version"]:
            raise LocalError("family_version_conflict")
        position = self.db.execute(
            "SELECT position FROM tenants WHERE tenant=?", (who["tenant"],)
        ).fetchone()[0]
        state, quarantine = self.k._state(who["tenant"], row["id"], position)
        if state != "accepted" or quarantine:
            raise LocalError("procedure_review_required")
        body = json.loads(row["body"])
        if action == "activate":
            points = selectors.bindings(args["bindings"])
            if any(m != "true" for m in self.matches(who, row, points, at, position)):
                raise LocalError("procedure_validation_required")
        elif self.activation(who["tenant"], row["id"], position) is None:
            raise LocalError("invalid_transition")
        detail = {
            "incarnation": self.s.incarnation,
            "policy": POLICY,
            "audience_hash": canonical_hash(body["readers"]),
            "reason": args["reason"],
            "actor": who["principal"],
            "validated_bindings": args.get("bindings", []),
            "execution_privileges": False,
        }
        self.s._apply(
            self.s._journal(
                {
                    "kind": "procedure",
                    "tenant": who["tenant"],
                    "actor": who["principal"],
                    "revision": row["id"],
                    "expected_hash": row["body_hash"],
                    "decision": action,
                    "detail": detail,
                }
            ),
            at,
        )
        return {
            "revision": row["id"],
            "family": row["family"],
            "family_version": row["family_version"] + 1,
            "state": "active" if action == "activate" else "suspended",
            "policy": POLICY,
            "execution_privileges": False,
        }

    def apply_policy(self, action: dict[str, Any], at: int) -> None:
        tenant = action["tenant"]
        row = self.db.execute(
            "SELECT * FROM revisions WHERE tenant=? AND id=?", (tenant, action["revision"])
        ).fetchone()
        if row is None or row["body_hash"] != action["expected_hash"]:
            self.db.execute(
                "UPDATE knowledge_state SET recovery_gap=1,generation=generation+1 WHERE tenant=?",
                (tenant,),
            )
            return
        position = self.s._event(
            tenant, "procedure_" + action["decision"], row["id"], at, action["actor"]
        )
        self.db.execute(
            "INSERT INTO lifecycle VALUES (?,?,?,?,?)",
            (tenant, row["id"], position, action["decision"], json.dumps(action["detail"])),
        )
        self.db.execute(
            "UPDATE families SET version=version+1 WHERE tenant=? AND id=?", (tenant, row["family"])
        )
        self.db.execute(
            "UPDATE knowledge_state SET generation=generation+1 WHERE tenant=?", (tenant,)
        )
        self.s._enqueue(tenant, position, None, action["actor"])

    def query(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        tenant, points = who["tenant"], selectors.bindings(args["bindings"])
        state = self.db.execute(
            "SELECT recovery_gap FROM knowledge_state WHERE tenant=?", (tenant,)
        ).fetchone()
        if state[0]:
            return {"status": "blocked", "reason": "knowledge_reconciliation_required", "items": []}
        position = self.db.execute(
            "SELECT position FROM tenants WHERE tenant=?", (tenant,)
        ).fetchone()[0]
        rows = self.db.execute(
            "SELECT r.*,f.project,f.version AS family_version FROM revisions r JOIN families f ON r.tenant=f.tenant AND r.family=f.id WHERE r.tenant=? AND f.project=? AND r.kind='procedure' ORDER BY r.created LIMIT 257",
            (tenant, args["project"]),
        ).fetchall()
        if len(rows) > LIMIT or len(points) > 32:
            return {"status": "blocked", "reason": "procedure_resolution_incomplete", "items": []}
        values: list[
            tuple[Any, dict[str, Any], str, bool, dict[str, Any] | None, bool, list[str]]
        ] = []
        work = 0
        for row in rows:
            if row["body"] is None:
                lifecycle, quarantine = self.k._state(tenant, row["id"], position)
                activation = self.activation(tenant, row["id"], position)
                values.append(
                    (row, {}, lifecycle, quarantine, activation, False, ["unknown"] * len(points))
                )
                continue
            body = json.loads(row["body"])
            if any(body[k] is not None and body[k] != args.get(k) for k in ("task", "run")):
                continue
            lifecycle, quarantine = self.k._state(tenant, row["id"], position)
            activation = self.activation(tenant, row["id"], position)
            available = self.k._available(who, row, at)
            masks = self.db.execute(
                "SELECT selector FROM replacements WHERE tenant=? AND predecessor=? AND position<=?",
                (tenant, row["id"], position),
            ).fetchall()
            work += len(points) * sum(
                len(clause["all_of"])
                for selector in [body["selector"], *[json.loads(m[0]) for m in masks]]
                for clause in selector["any_of"]
            )
            if work > 65536:
                return {
                    "status": "blocked",
                    "reason": "procedure_resolution_incomplete",
                    "items": [],
                }
            matches = self.matches(who, row, points, at, position)
            values.append((row, body, lifecycle, quarantine, activation, available, matches))
        items, blockers = [], []
        for row, body, lifecycle, quarantine, activation, available, matches in values:
            if not available:
                continue
            reason = None
            if lifecycle != "accepted":
                reason = "review_required" if lifecycle == "proposed" else lifecycle
            elif quarantine:
                reason = "quarantined"
            elif activation is None:
                reason = "activation_required"
            elif activation["action"] != "activate":
                reason = "suspended_by_review"
            elif activation["incarnation"] != self.s.incarnation:
                reason = "history_reconciliation_required"
            elif activation["policy"] != POLICY or activation["audience_hash"] != canonical_hash(
                body["readers"]
            ):
                reason = "activation_context_changed"
            elif any(m != "true" for m in matches):
                reason = "applicability_or_validation_unavailable"
            if reason is None:
                for other, obody, ostate, oq, oa, _, om in values:
                    if other["id"] == row["id"] or other["family"] != row["family"]:
                        continue
                    # Hidden/quarantined disagreement never becomes a clean active procedure.
                    if (
                        ostate == "accepted"
                        and oa
                        and oa["action"] == "activate"
                        and oa["incarnation"] == self.s.incarnation
                        and any(m != "false" for m in om)
                        and other["body_hash"] != row["body_hash"]
                    ):
                        reason = "unresolved_procedure_conflict"
                        break
            effective = (
                "active"
                if reason is None
                else "suspended"
                if lifecycle == "accepted" and activation
                else lifecycle
            )
            if reason is not None:
                blockers.append(
                    {"revision": row["id"], "reason": reason, "effective_state": effective}
                )
            if reason is not None and not args.get("diagnostic", False):
                continue
            proofs = [
                p
                for point in points
                if (p := Repository(self.s).proof(who, row, point, at)) is not None
            ]
            items.append(
                {
                    "revision": row["id"],
                    "family": row["family"],
                    "family_version": row["family_version"],
                    "body_hash": row["body_hash"],
                    "created_position": row["created"],
                    "origin": row["origin"],
                    "attestations": [],
                    "body": body,
                    "state": lifecycle,
                    "effective_state": effective,
                    "reason": reason,
                    "activation": activation,
                    "applicability": matches,
                    "code_validation": proofs,
                    "authority": "active_procedure" if reason is None else "procedure_inspection",
                    "execution_privileges": False,
                }
            )
        return {
            "status": "partial" if blockers else "complete",
            "items": items,
            "blockers": blockers,
            "recorded_position": position,
            "valid_ms": at,
            "policy": POLICY,
            "execution_privileges": False,
        }

    def verify_dependency(self, who: dict[str, Any], reference: str, at: int) -> None:
        try:
            revision, position, family_version = reference.split("|")
            row = self.k._revision(who["tenant"], revision)
            activation = self.activation(who["tenant"], revision, 9007199254740991)
            lifecycle, quarantine = self.k._state(who["tenant"], revision, 9007199254740991)
            if not self.k._available(who, row, at):
                raise LocalError("replay_denied")
            body = json.loads(row["body"])
            if (
                lifecycle != "accepted"
                or row["family_version"] != int(family_version)
                or quarantine
                or activation is None
                or activation["action"] != "activate"
                or activation["position"] != int(position)
                or activation["incarnation"] != self.s.incarnation
                or not temporal_valid(at, body["valid_start"], body["valid_end"])
                or self.db.execute(
                    "SELECT recovery_gap FROM knowledge_state WHERE tenant=?", (who["tenant"],)
                ).fetchone()[0]
            ):
                raise LocalError("replay_denied")
        except (ValueError, TypeError, LocalError) as exc:
            raise LocalError("replay_denied") from exc
