"""Bounded human inspection and preview-bound retention using current authority."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from memoryking.adapters.execution import Execution
from memoryking.adapters.experience_records import ExperienceRecords
from memoryking.adapters.handoff import Handoff
from memoryking.adapters.local_store import RETRY_MS, LocalStore, uid
from memoryking.adapters.retrieval import Retrieval
from memoryking.contracts.canonical import canonical_bytes, canonical_hash
from memoryking.domain.local import LocalError, Released
from memoryking.domain.usage import UsageRecord, summarize

VALIDATOR = Draft202012Validator(
    json.loads(Path(__file__).with_name("inspection.schema.json").read_text())
)


class Inspection:
    def __init__(self, store: LocalStore):
        self.s, self.db = store, store.db
        self.r = Retrieval(store)

    def execute(self, credential: str, command: dict[str, Any]) -> Released:
        if len(canonical_bytes(command)) > 131072 or not VALIDATOR.is_valid(command):
            raise LocalError("invalid_request")
        if command["incarnation"] != self.s.incarnation:
            raise LocalError("history_changed")
        action, args = command["action"], command["args"]
        with self.s.transaction() as decision:
            at = decision.evaluated_ms
            who = self.s._auth(credential, "read", args["project"], at)
            if action in {"operations", "purge", "retention_preview"}:
                self.s._auth(credential, "admin", "*", at)
            if action == "history":
                result = self.history(who, args, at)
            elif action == "operations":
                result = self.operations(who, args, at)
            elif action == "cost":
                result = self.cost(who, args, at)
            elif action == "handoffs":
                result = self.handoffs(who, args, at)
            elif action == "retention_preview":
                result = self.retention(who, args, at)
            else:
                result = self.purge(who, command, at)
            result.update(
                incarnation=self.s.incarnation,
                tenant=who["tenant"],
                principal=who["principal"],
                project=args["project"],
                evaluated_ms=at,
                fresh_until_ms=at + 15000,
                authorization="current",
                receipt=uid("rcpt"),
            )
            raw = canonical_bytes(result)
            if len(raw) > 524288:
                raise LocalError("inspection_response_limit")
            epochs = self.db.execute(
                "SELECT policy_epoch,purge_epoch FROM tenants WHERE tenant=?", (who["tenant"],)
            ).fetchone()
            self.db.execute(
                "INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    who["tenant"],
                    result["receipt"],
                    who["principal"],
                    self.s.incarnation,
                    at,
                    decision.deadline_ms,
                    *epochs,
                    hashlib.sha256(raw).hexdigest(),
                    "[]",
                ),
            )
            self.s.fault("after_validation")
        self.s._expose()
        if action == "purge":
            self.s.gc()
        return Released(raw, decision, self.s.clock, result["receipt"])

    def history(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        projection = Execution(self.s).snapshot(
            who, args["project"], at, args.get("recorded_position")
        )
        rows = self.db.execute(
            "SELECT * FROM checkpoints WHERE tenant=? AND project=? AND incarnation=? AND (readers='null' OR EXISTS(SELECT 1 FROM json_each(readers) WHERE value=?)) ORDER BY position DESC LIMIT 65",
            (who["tenant"], args["project"], self.s.incarnation, who["principal"]),
        ).fetchall()
        frames = []
        for row in rows[:64]:
            scope = {
                "project": args["project"],
                "task": row["task"],
                "run": None if row["stream"] == "task" else row["stream"][4:],
                "checkpoint": row["id"],
            }
            try:
                Execution(self.s).w._checkpoint(who, scope, at)
            except LocalError:
                continue
            frames.append(
                {
                    "position": row["position"],
                    "recorded_ms": row["created"],
                    "checkpoint": row["id"],
                    "task": row["task"],
                    "run": scope["run"],
                    "revision": row["revision"],
                }
            )
        return {
            "projection": projection,
            "frames": list(reversed(frames)),
            "coverage": "latest_64_audience_candidates_current_permission; missing_history_not_interpolated",
            "window_full": len(frames) >= 64,
            "external_actions": 0,
        }

    def operations(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        # Aggregate only jobs whose observation and dependency are currently visible.
        rows = self.db.execute(
            "SELECT j.*,e.at_ms FROM jobs j JOIN observations o ON j.tenant=o.tenant AND j.observation=o.id JOIN events e ON e.tenant=j.tenant AND e.position=j.position WHERE j.tenant=? AND o.project=? AND j.incarnation=? ORDER BY j.position DESC LIMIT 513",
            (who["tenant"], args["project"], self.s.incarnation),
        ).fetchall()
        items = []
        for row in rows[:512]:
            try:
                self.r._dependency(who, {"kind": "observation", "id": row["observation"]}, at)
            except LocalError:
                continue
            items.append(
                {
                    key: row[key]
                    for key in (
                        "id",
                        "position",
                        "state",
                        "attempts",
                        "fence",
                        "lease_until",
                        "available",
                    )
                }
            )
            items[-1]["age_ms"] = max(0, at - row["at_ms"])
        # Tenant frontiers are opaque ordering tokens, never hidden-item counts.
        state = self.db.execute("SELECT * FROM tenants WHERE tenant=?", (who["tenant"],)).fetchone()
        projection = self.db.execute(
            "SELECT * FROM projections WHERE tenant=?", (who["tenant"],)
        ).fetchone()
        return {
            "jobs": items,
            "counts": {
                status: sum(i["state"] == status for i in items)
                for status in sorted({i["state"] for i in items})
            },
            "coverage": "up_to_512_recent_jobs_with_current_visible_observation; non_observation_jobs_not_included",
            "frontier": {
                "source": state["position"],
                "projection": projection["frontier"],
                "generation": projection["generation"],
            },
            "authorization_epoch": state["policy_epoch"],
            "purge_epoch": state["purge_epoch"],
            "health": "request_authorized_primary_available",
            "lock_wait_ms": None,
            "cache_churn": None,
            "recovery_qualified": False,
            "controls": {
                "retry": "unsupported",
                "cancel": "unsupported",
                "schedule": "unsupported",
            },
        }

    def cost(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        rows = self.db.execute(
            "SELECT * FROM processing_usage WHERE tenant=? AND project=? AND principal=? AND incarnation=? AND expires>? ORDER BY created DESC,id LIMIT 513",
            (who["tenant"], args["project"], who["principal"], self.s.incarnation, at),
        ).fetchall()
        attempts = []
        for row in rows[:512]:
            try:
                for dep in json.loads(row["dependencies"]):
                    self.r._dependency(who, dep, at)
            except LocalError:
                continue
            attempts.append(
                UsageRecord(
                    *(
                        row[k]
                        for k in (
                            "id",
                            "provider",
                            "fingerprint",
                            "stage",
                            "status",
                            "input_tokens",
                            "cached_input_tokens",
                            "output_tokens",
                            "cost_microusd",
                            "duration_ms",
                        )
                    )
                )
            )
        summary = summarize(attempts, 0)
        summary.update(successful_tasks=None, tokens_per_successful_task=None)
        outcomes = ExperienceRecords(self.s).records(who, {**args, "kind": "outcome"}, at)
        return {
            "processing": summary,
            "attempts": [record.document() for record in attempts],
            "outcomes": outcomes,
            "coverage": "up_to_512_retained_own_current_authorized_processing_attempts; outcomes_are_separate_actor_reports_not_added_to_usage",
            "assurance": "provider_or_actor_reported_not_independent_metering",
            "full_task_cost": None,
            "human_effort": None,
            "unrecorded_attempts": "unknown",
            "savings": "not_evaluated",
            "estimated_usage": None,
            "duration_note": "attested_usage_duration_zero_is_unobserved_placeholder",
        }

    def handoffs(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        h = Handoff(self.s)
        rows = self.db.execute(
            "SELECT * FROM handoffs WHERE tenant=? AND project=? AND incarnation=? AND (sender=? OR recipient=?) ORDER BY created DESC,id LIMIT 65",
            (
                who["tenant"],
                args["project"],
                self.s.incarnation,
                who["principal"],
                who["principal"],
            ),
        ).fetchall()
        items = []
        for row in rows[:64]:
            try:
                h.w._checkpoint(who, h._scope(row), at)
            except LocalError:
                continue
            effective = row["state"]
            try:
                h._active(row, at)
            except LocalError as exc:
                effective = exc.code
            items.append(
                {
                    **{
                        key: row[key]
                        for key in (
                            "id",
                            "task",
                            "stream",
                            "checkpoint",
                            "sender",
                            "recipient",
                            "state",
                            "created",
                            "expires",
                            "stream_revision",
                        )
                    },
                    "effective": effective,
                    "role_label_is_authority": False,
                }
            )
        return {
            "items": items,
            "coverage": "up_to_64_own_handoffs_with_current_checkpoint_access",
            "external_actions": 0,
        }

    def retention(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM observations WHERE tenant=? AND project=? AND id=?",
            (who["tenant"], args["project"], args["observation"]),
        ).fetchone()
        if row is None or not self.s._visible(who, row, at):
            raise LocalError("unavailable")
        self.r._blob(row)
        rows = self.db.execute(
            "SELECT DISTINCT revision FROM evidence_spans WHERE tenant=? AND observation=? ORDER BY revision LIMIT 257",
            (who["tenant"], row["id"]),
        ).fetchall()
        affected = []
        for ref in rows[:256]:
            claim = self.r.k._revision(who["tenant"], ref["revision"])
            if self.r.k._available(who, claim, at):
                affected.append(
                    {
                        "revision": claim["id"],
                        "family": claim["family"],
                        "effect": "purged_with_source",
                    }
                )
        state = dict(
            self.db.execute("SELECT * FROM tenants WHERE tenant=?", (who["tenant"],)).fetchone()
        )
        preview = {
            "observation": row["id"],
            "content_hash": row["blob"],
            "affected": affected,
            "coverage": "up_to_256_direct_authorized_claims; dependent artifacts, checkpoints, handoffs and experience records are selected by recorded dependencies and not enumerated",
            "effects": [
                "Delete source and dependent claim bodies",
                "Retire rendered artifacts, checkpoints and handoffs that recorded a dependency on the source or its claims",
                "Delete access receipts that list the source; the purge epoch retires other retained views",
                "Retire ALL tenant replay results for working, handoff and experience operations (no dependency index)",
                "Retire experience records that recorded a dependency and disable ranking when any was retired",
                "Prior delivered copies and backups are not erased",
            ],
            "irreversible": True,
            "source_position": state["position"],
        }
        preview["preview"] = self.s._token(
            "retention_preview",
            tenant=who["tenant"],
            principal=who["principal"],
            project=args["project"],
            observation=row["id"],
            state=canonical_hash(state),
            digest=canonical_hash(preview),
            expires=at + 15000,
        )
        return preview

    def purge(self, who: dict[str, Any], command: dict[str, Any], at: int) -> dict[str, Any]:
        args = command["args"]
        lookup = (
            who["tenant"],
            self.s.incarnation,
            "dashboard-retention-v1",
            who["principal"],
            "purge",
            command["key"],
        )
        old = self.db.execute(
            "SELECT * FROM retries WHERE tenant=? AND incarnation=? AND namespace=? AND principal=? AND operation=? AND key=?",
            lookup,
        ).fetchone()
        if old is not None:
            if old["hash"] != canonical_hash(command):
                raise LocalError("idempotency_conflict")
            if old["result"] is None or at >= old["expires"]:
                raise LocalError("idempotency_result_expired")
            return {**json.loads(old["result"]), "replayed": True}
        token = self.s._decode(args["preview"], "retention_preview")
        state = dict(
            self.db.execute("SELECT * FROM tenants WHERE tenant=?", (who["tenant"],)).fetchone()
        )
        if (
            token.get("tenant") != who["tenant"]
            or token.get("principal") != who["principal"]
            or token.get("project") != args["project"]
            or token.get("observation") != args["observation"]
            or args["confirm"] != args["observation"]
            or at >= token.get("expires", 0)
            or token.get("state") != canonical_hash(state)
        ):
            raise LocalError("retention_preview_stale")
        current = self.retention(who, args, at)
        current.pop("preview")
        if token.get("digest") != canonical_hash(current):
            raise LocalError("retention_preview_stale")
        self.s._apply(
            self.s._journal(
                {
                    "kind": "purge",
                    "tenant": who["tenant"],
                    "actor": who["principal"],
                    "observation": args["observation"],
                }
            ),
            at,
        )
        result = {"observation": args["observation"], "status": "purged", "replayed": False}
        self.db.execute(
            "INSERT INTO retries VALUES (?,?,?,?,?,?,?,?,?,?)",
            (*lookup, canonical_hash(command), json.dumps(result), None, at + RETRY_MS),
        )
        return result
