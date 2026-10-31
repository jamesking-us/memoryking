"""Frozen, reviewed ranking experiments with real shadow retrieval and explicit rollback."""

from __future__ import annotations

import json
from typing import Any

from memoryking.adapters.experience_records import ExperienceRecords
from memoryking.adapters.local_store import LocalStore, uid
from memoryking.contracts.canonical import canonical_hash
from memoryking.domain.learning import POLICY, freeze, measure
from memoryking.domain.local import Decision, LocalError


class Ranking:
    def __init__(self, store: LocalStore):
        self.s, self.db, self.records = store, store.db, ExperienceRecords(store)

    def spec(self, who: dict[str, Any], project: str, reference: str, at: int) -> dict[str, Any]:
        row = self.records.get(who, project, reference, "ranking", at)
        body = json.loads(row["body"])
        if body["policy"] != POLICY:
            raise LocalError("ranking_policy_unsupported")
        return {
            "state": "experimental",
            "configuration": row["id"],
            "body_hash": row["body_hash"],
            "adjustments": body["adjustments"],
            "policy": POLICY,
            "quality": "development_only",
        }

    def active(self, who: dict[str, Any], project: str, at: int) -> dict[str, Any]:
        state = self.db.execute(
            "SELECT * FROM ranking_state WHERE tenant=? AND project=?", (who["tenant"], project)
        ).fetchone()
        if state is None or state["configuration"] is None:
            return {
                "state": "reconciliation_required"
                if state is not None and state["reconciliation"]
                else "disabled",
                "adjustments": {},
            }
        try:
            return self.spec(who, project, state["configuration"], at)
        except LocalError:
            # No hidden configuration, feedback IDs, scores or counts are disclosed.
            return {"state": "unavailable", "adjustments": {}}

    def propose(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        votes, deps = [], []
        for reference in args["feedback"]:
            row = self.records.get(who, args["project"], reference, "feedback", at)
            latest = self.db.execute(
                "SELECT max(revision) FROM experience_records WHERE tenant=? AND project=? AND principal=? AND incarnation=? AND kind='feedback' AND logical_key=?",
                (
                    who["tenant"],
                    args["project"],
                    row["principal"],
                    self.s.incarnation,
                    row["logical_key"],
                ),
            ).fetchone()[0]
            if latest != row["revision"]:
                raise LocalError("ranking_feedback_superseded")
            votes.append({**json.loads(row["body"]), "actor": row["principal"]})
            deps.append({"kind": "experience_record", "id": reference})
        body = {
            "policy": POLICY,
            "feedback": args["feedback"],
            "adjustments": freeze(votes, args["max_adjustment_ppm"]),
            "max_adjustment_ppm": args["max_adjustment_ppm"],
            "automatic_activation": False,
            "causal_claim": False,
            "quality": "unqualified_development",
        }
        return self.records.save(
            who, args["project"], "ranking", args["logical_key"], 0, body, deps, None, at
        )

    def evaluate(
        self, who: dict[str, Any], args: dict[str, Any], decision: Decision
    ) -> dict[str, Any]:
        from memoryking.adapters.retrieval import Retrieval

        at = decision.evaluated_ms
        spec = self.spec(who, args["project"], args["configuration"], at)
        r = Retrieval(self.s)
        deps = [{"kind": "experience_record", "id": args["configuration"]}]
        cases = []
        for case in args["cases"]:
            if case["brief"]["project"] != args["project"] or set(case["relevant"]) & set(
                case["forbidden"]
            ):
                raise LocalError("ranking_case_invalid")
            for reference in [*case["relevant"], *case["forbidden"]]:
                obs = self.db.execute(
                    "SELECT 1 FROM observations WHERE tenant=? AND project=? AND id=?",
                    (who["tenant"], args["project"], reference),
                ).fetchone()
                if obs is None:
                    r.k._revision(who["tenant"], reference, args["project"])
                dep = {"kind": "observation" if obs else "revision", "id": reference}
                r._dependency(who, dep, at)
                deps.append(dep)
            arms = {}
            for name, override in [
                ("baseline", {"state": "disabled", "adjustments": {}}),
                ("candidate", spec),
            ]:
                raw, diag, dependencies = r._brief(
                    who, case["brief"], decision, uid("shadow"), ranking_override=override
                )
                packet = json.loads(raw)
                selected = [item["reference"] for item in packet["guidance"]]
                arms[name] = {
                    "selected": selected,
                    "status": packet["status"],
                    "constraints_hash": canonical_hash(packet["constraints"]),
                    "output_hash": canonical_hash(packet),
                    "metrics": measure(selected, case["relevant"]),
                    "forbidden_selected": sorted(set(selected) & set(case["forbidden"])),
                    "tokens": diag["usage"]["output_tokens"],
                }
                deps.extend(dependencies)
            baseline, candidate = arms["baseline"], arms["candidate"]
            ok = (
                baseline["constraints_hash"] == candidate["constraints_hash"]
                and candidate["status"] not in {"blocked", "budget_insufficient"}
                and baseline["status"] not in {"blocked", "budget_insufficient"}
                and not candidate["forbidden_selected"]
                and all(
                    candidate["metrics"][metric] >= baseline["metrics"][metric]
                    for metric in ("recall_ppm", "precision_ppm")
                )
            )
            cases.append(
                {
                    "request": case["brief"],
                    "judgments": {"relevant": case["relevant"], "forbidden": case["forbidden"]},
                    "arms": arms,
                    "passes": ok,
                }
            )
        unique = {(dep["kind"], dep["id"]) for dep in deps}
        improved = any(
            case["arms"]["candidate"]["metrics"][metric]
            > case["arms"]["baseline"]["metrics"][metric]
            for case in cases
            for metric in ("recall_ppm", "precision_ppm")
        )
        body = {
            "configuration": args["configuration"],
            "configuration_hash": spec["body_hash"],
            "policy": POLICY,
            "dataset_kind": "development",
            "judgment_provenance": args["judgment_provenance"],
            "judgment_assurance": "actor_reported",
            "source_position": self._project_position(who["tenant"], args["project"]),
            "status": "passes_development_gates"
            if improved and all(case["passes"] for case in cases)
            else "fails_development_gates",
            "measured_incremental_value": improved,
            "cases": cases,
            "task_quality_qualified": False,
            "provider_calls": 0,
        }
        return self.records.save(
            who,
            args["project"],
            "shadow",
            args["logical_key"],
            0,
            body,
            [{"kind": k, "id": v} for k, v in sorted(unique)],
            None,
            at,
        )

    def _project_position(self, tenant: str, project: str) -> int:
        """Latest event that can change this project's ranking inputs.

        Captures, claims, reviews and feedback elsewhere in the tenant do not invalidate a
        shadow evaluation; the shadow record itself is excluded so a fresh evaluation stays
        activatable until the project changes.
        """
        statements = (
            "SELECT coalesce(max(position),0) FROM observations WHERE tenant=? AND project=?",
            "SELECT coalesce(max(position),0) FROM experience_records WHERE tenant=? AND project=? AND kind!='shadow'",
            "SELECT coalesce(max(r.created),0) FROM revisions r JOIN families f ON r.tenant=f.tenant AND r.family=f.id WHERE r.tenant=? AND f.project=?",
            "SELECT coalesce(max(l.position),0) FROM lifecycle l JOIN revisions r ON l.tenant=r.tenant AND l.revision=r.id JOIN families f ON r.tenant=f.tenant AND r.family=f.id WHERE l.tenant=? AND f.project=?",
        )
        return max(
            int(self.db.execute(statement, (tenant, project)).fetchone()[0])
            for statement in statements
        )

    def transition(
        self, who: dict[str, Any], args: dict[str, Any], action: str, at: int
    ) -> dict[str, Any]:
        state = self.db.execute(
            "SELECT * FROM ranking_state WHERE tenant=? AND project=?",
            (who["tenant"], args["project"]),
        ).fetchone()
        version = state["version"] if state is not None else 0
        if version != args["expected_version"]:
            raise LocalError("ranking_version_conflict")
        reference = args["configuration"]
        spec = self.spec(who, args["project"], reference, at) if reference is not None else None
        if action == "activate":
            shadow = self.records.get(who, args["project"], args["shadow"], "shadow", at)
            body = json.loads(shadow["body"])
            position = self._project_position(who["tenant"], args["project"])
            if (
                body["configuration"] != reference
                or spec is None
                or body["configuration_hash"] != spec["body_hash"]
                or body["status"] != "passes_development_gates"
            ):
                raise LocalError("ranking_evaluation_required")
            if position != body["source_position"]:
                raise LocalError("ranking_shadow_stale")
        elif (
            reference is not None
            and self.db.execute(
                "SELECT 1 FROM ranking_history WHERE tenant=? AND project=? AND configuration=?",
                (who["tenant"], args["project"], reference),
            ).fetchone()
            is None
        ):
            raise LocalError("ranking_not_previously_active")
        record = {
            "kind": "ranking",
            "tenant": who["tenant"],
            "project": args["project"],
            "actor": who["principal"],
            "configuration": reference,
            "expected_hash": spec["body_hash"] if spec else None,
            "version": version + 1,
            "decision": action,
            "reason": args["reason"],
            "shadow": args.get("shadow"),
            "incarnation": self.s.incarnation,
        }
        self.s._apply(self.s._journal(record), at)
        return {
            "state": "experimental" if reference else "disabled",
            "configuration": reference,
            "version": version + 1,
            "quality": "development_only",
            "automatic_online_learning": False,
        }

    def apply_policy(self, action: dict[str, Any], at: int) -> None:
        reference = action["configuration"]
        row = (
            self.db.execute(
                "SELECT body_hash FROM experience_records WHERE tenant=? AND id=? AND kind='ranking'",
                (action["tenant"], reference),
            ).fetchone()
            if reference
            else None
        )
        missing = reference is not None and (
            row is None or row["body_hash"] != action["expected_hash"]
        )
        selected = None if missing else reference
        position = self.s._event(
            action["tenant"],
            "ranking_" + action["decision"],
            action["project"],
            at,
            action["actor"],
        )
        self.db.execute(
            "INSERT INTO ranking_state VALUES (?,?,?,?,?) ON CONFLICT(tenant,project) DO UPDATE SET version=excluded.version,configuration=excluded.configuration,reconciliation=excluded.reconciliation",
            (action["tenant"], action["project"], action["version"], selected, int(missing)),
        )
        self.db.execute(
            "INSERT INTO ranking_history VALUES (?,?,?,?,?)",
            (action["tenant"], action["project"], position, selected, json.dumps(action)),
        )
        self.s._enqueue(action["tenant"], position, None, action["actor"])

    def inspect(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        state = self.db.execute(
            "SELECT * FROM ranking_state WHERE tenant=? AND project=?",
            (who["tenant"], args["project"]),
        ).fetchone()
        history = []
        for row in self.db.execute(
            "SELECT * FROM ranking_history WHERE tenant=? AND project=? ORDER BY position DESC LIMIT 32",
            (who["tenant"], args["project"]),
        ).fetchall():
            if row["configuration"]:
                try:
                    self.spec(who, args["project"], row["configuration"], at)
                except LocalError:
                    continue
            history.append(
                {
                    "position": row["position"],
                    "configuration": row["configuration"],
                    "decision": json.loads(row["detail"])["decision"],
                }
            )
        active = self.active(who, args["project"], at)
        return {
            "active": {key: value for key, value in active.items() if key != "adjustments"},
            "version": state["version"] if state else 0,
            "history": history,
            "coverage": "authorized_latest_32_transitions",
            "automatic_online_learning": False,
        }
