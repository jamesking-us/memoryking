"""Bounded human inspection and preview-bound retention using current authority."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from memoryking.adapters.execution import Execution
from memoryking.adapters.experience_records import ExperienceRecords
from memoryking.adapters.handoff import Handoff
from memoryking.adapters.local_store import RETRY_MS, uid
from memoryking.adapters.retrieval import Retrieval
from memoryking.application.store_core import StoreCore
from memoryking.contracts.canonical import canonical_bytes, canonical_hash
from memoryking.domain.local import LocalError, Released
from memoryking.domain.usage import UsageRecord, summarize

CATALOG_TEXT_BYTES = 196608  # text per catalog page, under the inspection reply limit
DAY_MS = 86_400_000
BACKLOG_ITEMS = 16  # oldest waiting proposals listed by the health report
HEALTH_MIN_SAMPLE = 5  # votes or briefings before a ratio becomes a signal
BACKLOG_ATTENTION_MS = 7 * DAY_MS  # a proposal waiting longer than this needs a reviewer
VALIDATOR = Draft202012Validator(
    json.loads(Path(__file__).with_name("inspection.schema.json").read_text())
)


class Inspection:
    def __init__(self, store: StoreCore):
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
            if action in {
                "operations",
                "purge",
                "retention_preview",
                "influence",
                "trust",
                "trust_list",
                "held",
                "release",
                "health",
            }:
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
            elif action == "influence":
                result = self.influence(who, args, at)
            elif action == "trust_list":
                result = self.trust_list(who, args, at)
            elif action == "held":
                result = self.held(who, args, at)
            elif action == "catalog":
                result = self.catalog(who, args, at)
            elif action == "health":
                result = self.health(who, args, at)
            elif action in {"trust", "release"}:
                result = self.policy(who, command, at)
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

    def influence(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        """Where one observation went: every record built from it and who received it.

        The same recorded dependencies a purge would follow, read without changing
        anything: the claims that cite the observation, the checkpoints, handoffs,
        summaries, experience records and retained deliveries (briefings, fetches,
        reruns, graph pages) that recorded a dependency on it or on anything derived
        from it, and the principals those reached. Bounded per list; truncation is
        explicit. Prior exports, provider deliveries and backups leave no record here."""
        tenant = who["tenant"]
        row = self.db.execute(
            "SELECT * FROM observations WHERE tenant=? AND id=? AND project=?",
            (tenant, args["observation"], args["project"]),
        ).fetchone()
        if row is None or row["deleted"]:
            raise LocalError("unavailable")
        self.r._blob(row)
        limit = 256
        truncation: set[str] = set()
        derived = {row["id"]}
        claims: list[dict[str, Any]] = []
        for ref in self.db.execute(
            "SELECT DISTINCT revision FROM evidence_spans WHERE tenant=? AND observation=? ORDER BY revision LIMIT 257",
            (tenant, row["id"]),
        ).fetchall()[: limit + 1]:
            if len(claims) >= limit:
                truncation.add("claims")
                break
            claim = self.r.k._revision(tenant, ref["revision"])
            derived.add(claim["id"])
            claims.append(
                {
                    "revision": claim["id"],
                    "family": claim["family"],
                    "kind": claim["kind"],
                    "issuer": claim["issuer"],
                    "readable_now": self.r.k._available(who, claim, at),
                }
            )

        def depends(raw: Any) -> bool:
            try:
                deps = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                return False
            return isinstance(deps, list) and any(
                isinstance(d, dict) and d.get("id") in derived for d in deps
            )

        checkpoints: dict[str, dict[str, Any]] = {}
        experience: dict[str, dict[str, Any]] = {}
        deliveries: dict[str, dict[str, Any]] = {}
        changed = True
        while changed:
            changed = False
            for item in self.db.execute(
                "SELECT id,task,stream,revision,writer,readers,created,state,dependencies FROM checkpoints WHERE tenant=? AND project=? AND state!='purged' ORDER BY created,id",
                (tenant, args["project"]),
            ).fetchall():
                if item["id"] in checkpoints or not depends(item["dependencies"]):
                    continue
                if len(checkpoints) >= limit:
                    truncation.add("checkpoints")
                    continue
                checkpoints[item["id"]] = {
                    "checkpoint": item["id"],
                    "task": item["task"],
                    "stream": item["stream"],
                    "revision": item["revision"],
                    "writer": item["writer"],
                    "readers": json.loads(item["readers"]),
                    "created_ms": item["created"],
                    "state": item["state"],
                }
                derived.add(item["id"])
                changed = True
            for item in self.db.execute(
                "SELECT id,dependencies FROM experience_records WHERE tenant=? AND body IS NOT NULL ORDER BY id",
                (tenant,),
            ).fetchall():
                if item["id"] in experience or not depends(item["dependencies"]):
                    continue
                if len(experience) >= limit:
                    truncation.add("experience")
                    continue
                experience[item["id"]] = {"record": item["id"]}
                derived.add(item["id"])
                changed = True
            for item in self.db.execute(
                "SELECT id,principal,created,state,diagnostics,dependencies FROM rendered_artifacts WHERE tenant=? AND project=? AND state='retained' ORDER BY created,id",
                (tenant, args["project"]),
            ).fetchall():
                if item["id"] in deliveries or not depends(item["dependencies"]):
                    continue
                if len(deliveries) >= limit:
                    truncation.add("deliveries")
                    continue
                diag = json.loads(item["diagnostics"] or "{}")
                deliveries[item["id"]] = {
                    "receipt": item["id"],
                    "kind": diag.get("kind", "brief"),
                    "principal": item["principal"],
                    "created_ms": item["created"],
                    "status": diag.get("status"),
                }
                derived.add(item["id"])
                changed = True
        handoffs: list[dict[str, Any]] = []
        summaries: list[dict[str, Any]] = []
        for identifier in checkpoints:
            for item in self.db.execute(
                "SELECT id,sender,recipient,role,stage,state,created FROM handoffs WHERE tenant=? AND checkpoint=? ORDER BY created,id",
                (tenant, identifier),
            ).fetchall():
                if len(handoffs) >= limit:
                    truncation.add("handoffs")
                    break
                handoffs.append(
                    {
                        "handoff": item["id"],
                        "checkpoint": identifier,
                        "sender": item["sender"],
                        "recipient": item["recipient"],
                        "role": item["role"],
                        "stage": item["stage"],
                        "state": item["state"],
                        "created_ms": item["created"],
                    }
                )
        for item in self.db.execute(
            "SELECT id,checkpoint,principal,method,readers,created FROM session_summaries WHERE tenant=? AND project=? ORDER BY created,id",
            (tenant, args["project"]),
        ).fetchall():
            if item["checkpoint"] not in checkpoints and item["id"] not in deliveries:
                continue
            if len(summaries) >= limit:
                truncation.add("summaries")
                break
            summaries.append(
                {
                    "summary": item["id"],
                    "checkpoint": item["checkpoint"],
                    "principal": item["principal"],
                    "method": item["method"],
                    "readers": json.loads(item["readers"]),
                    "created_ms": item["created"],
                }
            )
        principals: set[str] = {d["principal"] for d in deliveries.values()}
        for value in checkpoints.values():
            principals.update(value["readers"])
        for value in handoffs:
            principals.update((value["sender"], value["recipient"]))
        for value in summaries:
            principals.update(value["readers"])
        state = self.db.execute("SELECT position FROM tenants WHERE tenant=?", (tenant,)).fetchone()
        return {
            "observation": row["id"],
            "source": row["source"],
            "captured_by": row["principal"],
            "position": row["position"],
            "content_hash": row["blob"],
            "claims": claims,
            "checkpoints": list(checkpoints.values()),
            "handoffs": handoffs,
            "summaries": summaries,
            "experience": list(experience.values()),
            "deliveries": list(deliveries.values()),
            "principals": sorted(principals),
            "counts": {
                "claims": len(claims),
                "checkpoints": len(checkpoints),
                "handoffs": len(handoffs),
                "summaries": len(summaries),
                "experience": len(experience),
                "deliveries": len(deliveries),
                "principals": len(principals),
            },
            "truncation": sorted(truncation),
            "coverage": "recorded_dependencies_in_this_project; retained_deliveries_only; prior_exports_provider_deliveries_and_backups_not_tracked",
            "source_position": state["position"],
        }

    def health(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        """Whether memory is paying for itself, from records the store already keeps.

        Counts over one project and a trailing window: helpful feedback against every
        rated vote, briefings that found no relevant memory, observations no retained
        delivery ever included, claims retired because their evidence was purged or
        replaced by a successor, and how long the oldest proposal has waited. Every
        figure is a count or a ratio of counts; no text and no principal is named, so
        the report reveals usage, not content. Admin only, because it aggregates every
        principal's feedback and deliveries in the project. Each signal says what the
        numbers suggest and the thresholds are fixed and documented, not learned."""
        tenant, project = who["tenant"], args["project"]
        days = args.get("days", 30)
        since = at - days * DAY_MS
        frontier = self.db.execute(
            "SELECT position FROM tenants WHERE tenant=?", (tenant,)
        ).fetchone()[0]

        def count(sql: str, *values: Any) -> int:
            return int(self.db.execute(sql, (tenant, project, *values)).fetchone()[0])

        observations: dict[str, Any] = {
            "total": count(
                "SELECT count(*) FROM observations WHERE tenant=? AND project=? AND deleted=0"
            ),
            "recent": count(
                "SELECT count(*) FROM observations o JOIN events e ON e.tenant=o.tenant AND e.position=o.position WHERE o.tenant=? AND o.project=? AND o.deleted=0 AND e.at_ms>=?",
                since,
            ),
            "held": count(
                "SELECT count(*) FROM observation_holds h JOIN observations o ON o.tenant=h.tenant AND o.id=h.id WHERE h.tenant=? AND h.project=? AND h.state='held' AND o.deleted=0"
            ),
            "purged": count(
                "SELECT count(*) FROM observations WHERE tenant=? AND project=? AND deleted=1"
            ),
        }
        # Retained deliveries in the window: briefings (and reruns) by status, every kind
        # by count, and the observations any of them carried.
        by_kind: dict[str, int] = {}
        by_status: dict[str, int] = {}
        carried: set[str] = set()
        for row in self.db.execute(
            "SELECT diagnostics,dependencies FROM rendered_artifacts WHERE tenant=? AND project=? AND state='retained' AND created>=?",
            (tenant, project, since),
        ).fetchall():
            diag = json.loads(row["diagnostics"] or "{}")
            kind = str(diag.get("kind") or diag.get("operation") or "brief")
            by_kind[kind] = by_kind.get(kind, 0) + 1
            if kind == "brief":
                status = str(diag.get("status", "unknown"))
                by_status[status] = by_status.get(status, 0) + 1
            for dep in json.loads(row["dependencies"] or "[]"):
                if (
                    isinstance(dep, dict)
                    and dep.get("kind") == "observation"
                    and isinstance(dep.get("id"), str)
                ):
                    carried.add(dep["id"])
        delivered = 0
        ids = sorted(carried)
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            delivered += count(
                "SELECT count(*) FROM observations WHERE tenant=? AND project=? AND deleted=0 AND id IN ("
                + ",".join("?" for _ in chunk)
                + ")",
                *chunk,
            )
        observations["delivered"] = delivered
        observations["never_delivered"] = observations["total"] - delivered
        observations["never_delivered_ppm"] = (
            observations["never_delivered"] * 1_000_000 // observations["total"]
            if observations["total"]
            else None
        )
        briefings_total = sum(by_status.values())
        empty = by_status.get("no_relevant_memory", 0)
        briefings = {
            "total": briefings_total,
            "by_status": dict(sorted(by_status.items())),
            "no_relevant_memory": empty,
            "no_relevant_memory_ppm": (
                empty * 1_000_000 // briefings_total if briefings_total else None
            ),
            "deliveries": sum(by_kind.values()),
            "deliveries_by_kind": dict(sorted(by_kind.items())),
        }
        # Feedback: the latest revision of each vote, counted once, in the window.
        latest: dict[tuple[str, str, str], tuple[int, int, dict[str, Any]]] = {}
        for row in self.db.execute(
            "SELECT e.principal,e.incarnation,e.logical_key,e.revision,e.body,ev.at_ms FROM experience_records e JOIN events ev ON ev.tenant=e.tenant AND ev.position=e.position WHERE e.tenant=? AND e.project=? AND e.kind='feedback' AND e.body IS NOT NULL",
            (tenant, project),
        ).fetchall():
            key = (row["principal"], row["incarnation"], row["logical_key"])
            if key not in latest or latest[key][0] < row["revision"]:
                latest[key] = (row["revision"], row["at_ms"], json.loads(row["body"]))
        by_usefulness: dict[str, int] = {}
        by_use: dict[str, int] = {}
        for _, at_ms, body in latest.values():
            if at_ms < since:
                continue
            usefulness = str(body.get("usefulness", "unknown"))
            by_usefulness[usefulness] = by_usefulness.get(usefulness, 0) + 1
            use = str(body.get("use", "unknown"))
            by_use[use] = by_use.get(use, 0) + 1
        votes = sum(by_usefulness.values())
        rated = votes - by_usefulness.get("unknown", 0)
        helpful = by_usefulness.get("helpful", 0)
        feedback = {
            "votes": votes,
            "rated": rated,
            "helpful": helpful,
            "helpful_ppm": helpful * 1_000_000 // rated if rated else None,
            "by_usefulness": dict(sorted(by_usefulness.items())),
            "by_use": dict(sorted(by_use.items())),
        }
        # Claims: the latest revision of every non-procedure family, purged ones included,
        # so a family retired because its evidence was purged still counts.
        by_state: dict[str, int] = {}
        quarantined = 0
        evidence_purged = 0
        families = 0
        waiting: list[dict[str, Any]] = []
        for row in self.db.execute(
            "SELECT r.id,r.family,r.kind,r.issuer,r.purged,f.logical_key,ev.at_ms FROM revisions r JOIN families f ON f.tenant=r.tenant AND f.id=r.family JOIN events ev ON ev.tenant=r.tenant AND ev.position=r.created WHERE r.tenant=? AND f.project=? AND r.kind!='procedure' AND r.created=(SELECT max(r2.created) FROM revisions r2 WHERE r2.tenant=r.tenant AND r2.family=r.family) ORDER BY r.created,r.id",
            (tenant, project),
        ).fetchall():
            families += 1
            if row["purged"]:
                evidence_purged += 1
                continue
            state, quarantine = self.r.k._state(tenant, row["id"], frontier)
            by_state[state] = by_state.get(state, 0) + 1
            if quarantine:
                quarantined += 1
            if state == "proposed":
                waiting.append(
                    {
                        "revision": row["id"],
                        "family": row["family"],
                        "logical_key": row["logical_key"],
                        "kind": row["kind"],
                        "issuer": row["issuer"],
                        "quarantined": quarantine,
                        "age_ms": max(0, at - row["at_ms"]),
                    }
                )
        superseded = count(
            "SELECT count(DISTINCT rp.predecessor) FROM replacements rp JOIN revisions r ON r.tenant=rp.tenant AND r.id=rp.predecessor JOIN families f ON f.tenant=r.tenant AND f.id=r.family WHERE rp.tenant=? AND f.project=?"
        )
        claims = {
            "families": families,
            "by_state": dict(sorted(by_state.items())),
            "quarantined": quarantined,
            "evidence_purged": evidence_purged,
            "superseded": superseded,
        }
        waiting.sort(key=lambda item: (-item["age_ms"], item["revision"]))
        backlog = {
            "proposed": len(waiting),
            "oldest_age_ms": waiting[0]["age_ms"] if waiting else None,
            "items": waiting[:BACKLOG_ITEMS],
            "truncated": len(waiting) > BACKLOG_ITEMS,
        }
        signals: list[dict[str, str]] = []

        def signal(name: str, level: str, text: str) -> None:
            signals.append({"name": name, "level": level, "text": text})

        if rated < HEALTH_MIN_SAMPLE:
            signal(
                "feedback",
                "unknown",
                f"{rated} rated vote(s) in {days} day(s); at least {HEALTH_MIN_SAMPLE} are needed before the helpful ratio means anything.",
            )
        elif by_usefulness.get("harmful", 0) or by_usefulness.get("misleading", 0):
            signal(
                "feedback",
                "attention",
                f"{by_usefulness.get('harmful', 0)} harmful and {by_usefulness.get('misleading', 0)} misleading vote(s): look at what was delivered and consider quarantine.",
            )
        elif helpful * 2 < rated:
            signal(
                "feedback",
                "watch",
                f"{helpful} of {rated} rated votes were helpful: under half of what memory delivered was worth reading.",
            )
        else:
            signal("feedback", "ok", f"{helpful} of {rated} rated votes were helpful.")
        if briefings_total < HEALTH_MIN_SAMPLE:
            signal(
                "briefings",
                "unknown",
                f"{briefings_total} retained briefing(s) in {days} day(s); at least {HEALTH_MIN_SAMPLE} are needed before the empty share means anything.",
            )
        elif empty * 2 > briefings_total:
            signal(
                "briefings",
                "watch",
                f"{empty} of {briefings_total} briefings found no relevant memory: agents ask about things nothing was captured for.",
            )
        elif by_status.get("budget_insufficient", 0) * 4 > briefings_total:
            signal(
                "briefings",
                "watch",
                f"{by_status.get('budget_insufficient', 0)} of {briefings_total} briefings ran out of output budget: raise max_output_tokens or ask narrower questions.",
            )
        else:
            signal(
                "briefings",
                "ok",
                f"{briefings_total - empty} of {briefings_total} briefings carried relevant memory.",
            )
        if not waiting:
            signal("backlog", "ok", "No proposal is waiting for review.")
        elif waiting[0]["age_ms"] > BACKLOG_ATTENTION_MS:
            signal(
                "backlog",
                "attention",
                f"{len(waiting)} proposal(s) waiting; the oldest for {waiting[0]['age_ms'] // DAY_MS} day(s). Reviewed knowledge is not growing.",
            )
        else:
            signal("backlog", "watch", f"{len(waiting)} proposal(s) waiting for review.")
        if quarantined:
            signal(
                "evidence",
                "attention",
                f"{quarantined} claim(s) quarantined; {evidence_purged} retired because evidence was purged; {superseded} replaced.",
            )
        elif evidence_purged:
            signal(
                "evidence",
                "watch",
                f"{evidence_purged} claim(s) retired because their evidence was purged; propose again from evidence that remains. {superseded} replaced.",
            )
        else:
            signal(
                "evidence",
                "ok",
                f"No claim lost its evidence; {superseded} replaced by a successor.",
            )
        if observations["total"] < 4 * HEALTH_MIN_SAMPLE:
            signal(
                "reach",
                "unknown",
                f"{observations['total']} observation(s); too few to judge how much of memory is read.",
            )
        elif observations["never_delivered"] * 10 > observations["total"] * 9:
            signal(
                "reach",
                "watch",
                f"{observations['never_delivered']} of {observations['total']} observations were in no retained delivery in {days} day(s): memory is written far more than it is read.",
            )
        else:
            signal(
                "reach",
                "ok",
                f"{delivered} of {observations['total']} observations reached a retained delivery in {days} day(s).",
            )
        return {
            "window": {"days": days, "since_ms": since},
            "observations": observations,
            "briefings": briefings,
            "feedback": feedback,
            "claims": claims,
            "backlog": backlog,
            "signals": signals,
            "coverage": "counts_only_no_text_no_principals; retained_deliveries_in_window_only; latest_vote_per_actor_and_item; latest_revision_per_family; thresholds_fixed_not_learned",
            "source_position": frontier,
        }

    def trust_list(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        from memoryking.adapters.trust import Trust

        return {"rules": Trust(self.s).rules(who["tenant"], args["project"])}

    def catalog(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        """The project's records as a catalog, newest first, paged by position.

        `observations` lists every visible capture (optionally one source, one file
        name with its versions, or one id); `claims` lists the latest unpurged
        `knowledge` revision of every family, accepted ones for every reader and the
        rest only for a reviewer. Text is included unless `text` is false, within a
        page budget, so the memory-tool adapter and the vault export can read a whole
        project without a model and without a briefing. Reads here leave no delivery
        record; `fetch` is the receipted way to open one record."""
        if args.get("kind", "observations") == "claims":
            return self._claim_catalog(who, args, at)
        return self._observation_catalog(who, args, at)

    def _observation_catalog(
        self, who: dict[str, Any], args: dict[str, Any], at: int
    ) -> dict[str, Any]:
        where = ["o.tenant=?", "o.project=?", "o.deleted=0"]
        values: list[Any] = [who["tenant"], args["project"]]
        if "source" in args:
            where.append("o.source=?")
            values.append(args["source"])
        if "name" in args:
            # A file's versions share its name: `name`, then `name__v2`, `name__v3`, ...
            where.append("(o.source_event=? OR o.source_event LIKE ? ESCAPE '!')")
            values.extend(
                [args["name"], args["name"].replace("!", "!!").replace("_", "!_") + "!_!_v%"]
            )
        if "id" in args:
            where.append("o.id=?")
            values.append(args["id"])
        total = self.db.execute(
            "SELECT count(*) FROM observations o WHERE " + " AND ".join(where), values
        ).fetchone()[0]
        if "after" in args:
            where.append("o.position<?")
            values.append(args["after"])
        limit = args.get("limit", 256)
        rows = self.db.execute(
            "SELECT o.*,e.at_ms AS recorded_ms FROM observations o JOIN events e ON e.tenant=o.tenant AND e.position=o.position WHERE "
            + " AND ".join(where)
            + " ORDER BY o.position DESC,o.id DESC LIMIT ?",
            (*values, limit + 1),
        ).fetchall()
        version = re.compile(r"^(.+?)__v[0-9]+$")
        items: list[dict[str, Any]] = []
        used = 0
        next_after: int | None = None
        for index, row in enumerate(rows):
            if index >= limit:
                next_after = rows[index - 1]["position"]
                break
            if not self.s._visible(who, row, at):
                continue
            if "name" in args:
                match = version.match(row["source_event"])
                if (match.group(1) if match else row["source_event"]) != args["name"]:
                    continue
            raw = self.r._blob(row)
            if items and args.get("text", True) and used + len(raw) > CATALOG_TEXT_BYTES:
                next_after = row["position"] + 1
                break
            used += len(raw)
            item = {
                "id": row["id"],
                "source": row["source"],
                "source_event": row["source_event"],
                "principal": row["principal"],
                "position": row["position"],
                "recorded_ms": row["recorded_ms"],
                "observed_at_ms": row["observed_at_ms"],
                "classification": row["classification"],
                "readers": json.loads(row["readers"]),
                "bytes": len(raw),
                "hash": row["blob"],
            }
            if args.get("text", True):
                item["text"] = raw.decode("utf-8", "replace")
            items.append(item)
        return {
            "kind": "observations",
            "items": items,
            "total": total,
            "next_after": next_after,
            "coverage": "visible_observations_newest_first; one_page_per_call; text_within_page_budget",
        }

    def _claim_catalog(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        tenant = who["tenant"]
        try:
            self.s._subject_auth(
                self.s._token("credential", tenant=tenant, principal=who["principal"]),
                "review",
                args["project"],
                at,
            )
            reviewer = True
        except LocalError:
            reviewer = False
        frontier = self.db.execute(
            "SELECT position FROM tenants WHERE tenant=?", (tenant,)
        ).fetchone()[0]
        where = [
            "r.tenant=?",
            "f.project=?",
            "r.kind='knowledge'",
            "r.purged=0",
            "r.created=(SELECT max(r2.created) FROM revisions r2 WHERE r2.tenant=r.tenant AND r2.family=r.family AND r2.purged=0)",
        ]
        values: list[Any] = [tenant, args["project"]]
        if "name" in args:
            where.append("f.logical_key=?")
            values.append(args["name"])
        if "id" in args:
            where.append("r.id=?")
            values.append(args["id"])
        total = self.db.execute(
            "SELECT count(*) FROM revisions r JOIN families f ON r.tenant=f.tenant AND r.family=f.id WHERE "
            + " AND ".join(where),
            values,
        ).fetchone()[0]
        if "after" in args:
            where.append("r.created<?")
            values.append(args["after"])
        limit = args.get("limit", 256)
        rows = self.db.execute(
            "SELECT r.*,f.project,f.logical_key,f.version AS family_version FROM revisions r JOIN families f ON r.tenant=f.tenant AND r.family=f.id WHERE "
            + " AND ".join(where)
            + " ORDER BY r.created DESC LIMIT ?",
            (*values, limit + 1),
        ).fetchall()
        items: list[dict[str, Any]] = []
        used = 0
        next_after: int | None = None
        for index, row in enumerate(rows):
            if index >= limit:
                next_after = rows[index - 1]["created"]
                break
            state, quarantined = self.r.k._state(tenant, row["id"], frontier)
            if (state != "accepted" or quarantined) and not reviewer:
                continue
            if not self.r.k._available(who, row, at):
                continue
            body = json.loads(row["body"])
            size = len(body["text"].encode())
            if items and args.get("text", True) and used + size > CATALOG_TEXT_BYTES:
                next_after = row["created"] + 1
                break
            used += size
            item = {
                "revision": row["id"],
                "family": row["family"],
                "logical_key": row["logical_key"],
                "family_version": row["family_version"],
                "state": state,
                "quarantined": quarantined,
                "kind": row["kind"],
                "created": row["created"],
                "issuer": row["issuer"],
                "bytes": size,
                "hash": hashlib.sha256(body["text"].encode()).hexdigest(),
                "valid_start": body["valid_start"],
                "valid_end": body["valid_end"],
                "readers": body["readers"],
                "evidence": sorted(
                    {
                        span["observation"]
                        for span in self.db.execute(
                            "SELECT observation FROM evidence_spans WHERE tenant=? AND revision=?",
                            (tenant, row["id"]),
                        ).fetchall()
                    }
                ),
                "provenance": body.get("provenance", {}),
            }
            if args.get("text", True):
                item["text"] = body["text"]
            items.append(item)
        return {
            "kind": "claims",
            "items": items,
            "total": total,
            "next_after": next_after,
            "reviewer": reviewer,
            "coverage": "latest_unpurged_knowledge_revision_per_family_newest_first; accepted_for_readers; every_state_for_reviewers",
        }

    def held(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        from memoryking.adapters.trust import Trust

        items = Trust(self.s).listing(who, args["project"])
        return {
            "held": items,
            "coverage": "oldest_64_held_captures_in_this_project; preview_is_the_first_256_bytes",
        }

    def policy(self, who: dict[str, Any], command: dict[str, Any], at: int) -> dict[str, Any]:
        """A trust rule or a release is a journaled policy decision with a retry key."""
        action, args = command["action"], command["args"]
        lookup = (
            who["tenant"],
            self.s.incarnation,
            "dashboard-trust-v1",
            who["principal"],
            action,
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
        record: dict[str, Any] = {"tenant": who["tenant"], "actor": who["principal"]}
        if action == "trust":
            record.update(
                kind="trust",
                project=args["project"],
                source=args["source"],
                level=args["level"],
                explicit=args.get("explicit", True),
            )
            result = {
                "project": args["project"],
                "source": args["source"],
                "level": args["level"],
                "status": "recorded",
                "replayed": False,
            }
        else:
            row = self.db.execute(
                "SELECT 1 FROM observation_holds WHERE tenant=? AND id=? AND project=? AND state='held'",
                (who["tenant"], args["observation"], args["project"]),
            ).fetchone()
            if row is None:
                raise LocalError("hold_unavailable")
            record.update(kind="hold", observation=args["observation"])
            result = {"observation": args["observation"], "status": "released", "replayed": False}
        self.s._apply(self.s._journal(record), at)
        self.db.execute(
            "INSERT INTO retries VALUES (?,?,?,?,?,?,?,?,?,?)",
            (*lookup, canonical_hash(command), json.dumps(result), None, at + RETRY_MS),
        )
        return result

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
