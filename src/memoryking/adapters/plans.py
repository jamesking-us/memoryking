"""Structured plans reuse checkpoint durability; callers hold the canonical transaction."""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any

from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import LocalError
from memoryking.domain.plans import definition, progress, proof_refs, validate

if TYPE_CHECKING:
    from memoryking.adapters.working import Working


class Plans:
    def __init__(self, working: Working):
        self.w, self.s, self.db = working, working.s, working.db

    def scope(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"project": args["project"], "task": args["task"], "run": None}

    def current(self, who: dict[str, Any], args: dict[str, Any], at: int) -> Any:
        row = self.w._checkpoint(who, {**self.scope(args), "checkpoint": None}, at)
        if "plan" not in json.loads(row["body"]):
            raise LocalError("plan_unavailable")
        return row

    def preflight(self, who: dict[str, Any], action: str, args: dict[str, Any], at: int) -> None:
        if action not in {"plan_save", "plan_step"}:
            return
        self.w._writer(who, self.scope(args), create=action == "plan_save")
        if action == "plan_save":
            if who["principal"] not in args["readers"]:
                raise LocalError("checkpoint_writer_audience_required")
            validate(args["plan"])
            for dep in self.dependencies(args["plan"]):
                self.w._dependency(who, dep, at)
        else:
            self.current(who, args, at)
            for dep in args["evidence"]:
                self.w._dependency(who, dep, at)

    @staticmethod
    def dependencies(plan: dict[str, Any]) -> list[dict[str, str]]:
        deps = list(plan["lessons"])
        for step in plan["steps"]:
            deps.extend(step["evidence"])
        return deps

    def save(
        self, who: dict[str, Any], action: str, args: dict[str, Any], at: int
    ) -> dict[str, Any]:
        scope = self.scope(args)
        if action == "plan_step":
            row = self.current(who, args, at)
            body = json.loads(row["body"])
            readers = json.loads(row["readers"])
        else:
            body = {
                "objective": args["plan"]["objective"],
                "acceptance": [],
                "hard_constraints": [],
                "current_state": "Structured plan",
                "repository": {
                    "repository": None,
                    "base_commit": None,
                    "current_commit": None,
                    "dirty": False,
                    "artifacts": [],
                },
                "findings": [],
                "failed_attempts": [],
                "verification": [],
                "blockers": [],
                "next_actions": [],
                "references": [],
                "operation_ids": [],
                "capture": {"last_event": None, "observed_ms": at, "gaps": []},
            }
            readers = args["readers"]
        if action == "plan_step":
            step = next((s for s in body["plan"]["steps"] if s["id"] == args["step"]), None)
            if step is None:
                raise LocalError("plan_step_unavailable")
            for key in ("status", "notes", "evidence", "decision"):
                step[key] = copy.deepcopy(args[key])
        else:
            body["plan"] = copy.deepcopy(args["plan"])
            readers = args["readers"]
        body["objective"] = body["plan"]["objective"]
        body["current_state"] = "Structured plan: " + body["plan"]["status"]
        body["capture"]["observed_ms"] = at
        self.w._body(who, body, at)
        return self.w._save(
            who,
            {
                **scope,
                "checkpoint": body,
                "readers": readers,
                "expected_revision": args["expected_revision"],
            },
            at,
        )

    def get(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        current = self.current(who, args, at)
        stream = self.w._stream(who, self.scope(args))
        assert stream is not None
        row = current
        revision = args.get("revision")
        if revision is not None and revision != current["revision"]:
            candidate = self.db.execute(
                "SELECT id FROM checkpoints WHERE tenant=? AND project=? AND task=? AND stream='task' AND incarnation=? AND revision=?",
                (who["tenant"], args["project"], args["task"], self.s.incarnation, revision),
            ).fetchone()
            if candidate is None:
                raise LocalError("plan_unavailable")
            row = self.w._checkpoint(who, {**self.scope(args), "checkpoint": candidate["id"]}, at)
        plan = copy.deepcopy(json.loads(row["body"])["plan"])
        history = self.db.execute(
            "SELECT id FROM checkpoints WHERE tenant=? AND project=? AND task=? AND stream='task' AND incarnation=? AND revision<? ORDER BY revision DESC LIMIT 4097",
            (who["tenant"], args["project"], args["task"], self.s.incarnation, row["revision"]),
        ).fetchall()
        complete = len(history) <= 4096
        prior_plans = []
        for item in history[:4096]:
            try:
                previous = self.w._checkpoint(
                    who, {**self.scope(args), "checkpoint": item["id"]}, at
                )
                prior_plans.append(json.loads(previous["body"])["plan"])
            except (LocalError, KeyError):
                complete = False
        # Compare sets, not ordering: dropping/reordering/duplicating old links is
        # not fresh proof. Compute all definitions before changing effective states.
        definitions = {
            s["id"]: definition(plan, s["id"]) for s in plan["steps"] if s["status"] == "done"
        }
        old_by_plan = [(p, {s["id"]: s for s in p["steps"]}) for p in prior_plans]
        for step in plan["steps"]:
            step["reported_status"] = step["status"]
            if step["status"] == "done":
                superseded: set[tuple[str, str]] = set()
                for prior, old_steps in old_by_plan:
                    old = old_steps.get(step["id"])
                    if (
                        old
                        and old["status"] == "done"
                        and definition(prior, step["id"]) != definitions[step["id"]]
                    ):
                        superseded.update(proof_refs(old))
                if not complete or not (proof_refs(step) - superseded):
                    step["status"] = "unknown"
        # Unknown prerequisite proof cannot leave dependent work counted as done.
        changed = True
        while changed:
            changed = False
            by_id = {s["id"]: s for s in plan["steps"]}
            for step in plan["steps"]:
                if step["status"] == "done" and any(
                    by_id[d]["status"] not in {"done", "skipped"} for d in step["depends_on"]
                ):
                    step["status"] = "unknown"
                    changed = True
        return {
            "id": args["task"],
            "revision": row["revision"],
            "current_revision": stream["revision"],
            "checkpoint": row["id"],
            "writer": stream["writer"],
            "updated_ms": row["created"],
            "plan": plan,
            "progress": progress(plan),
            "history_complete": complete,
            "authority": "recorded_work_with_current_evidence_access_not_independent_certification",
        }

    def listing(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        epoch = self.db.execute(
            "SELECT position,policy_epoch,purge_epoch FROM tenants WHERE tenant=?", (who["tenant"],)
        ).fetchone()
        binding = {
            "tenant": who["tenant"],
            "principal": who["principal"],
            "project": args["project"],
            "frontier": epoch[0],
            "epoch": epoch[1],
            "purge": epoch[2],
            "limit": args["limit"],
        }
        after = ""
        if args["cursor"] is not None:
            token = self.s._decode(args["cursor"], "plan_cursor")
            if any(token.get(k) != v for k, v in binding.items()) or token["expires"] <= at:
                raise LocalError("context_changed")
            after = token["after"]
        rows = self.db.execute(
            "SELECT task,latest FROM checkpoint_streams WHERE tenant=? AND project=? AND stream='task' AND incarnation=? AND task>? ORDER BY task LIMIT 65",
            (who["tenant"], args["project"], self.s.incarnation, after),
        ).fetchall()
        result: list[dict[str, Any]] = []
        inspected = 0
        for row in rows[:64]:
            before = after
            inspected += 1
            after = row["task"]
            raw = self.db.execute(
                "SELECT body,readers,state FROM checkpoints WHERE tenant=? AND id=?",
                (who["tenant"], row["latest"]),
            ).fetchone()
            if (
                raw is None
                or raw["state"] != "available"
                or who["principal"] not in json.loads(raw["readers"])
            ):
                continue
            if "plan" not in json.loads(raw["body"]):
                continue
            item = self.get(
                who, {"project": args["project"], "task": row["task"], "revision": None}, at
            )
            plan = item.pop("plan")
            item.update(title=plan["title"], objective=plan["objective"], status=plan["status"])
            if result and len(canonical_bytes([*result, item])) > 98304:
                after = before
                inspected -= 1
                break
            result.append(item)
            if len(result) >= args["limit"]:
                break
        more = inspected < len(rows)
        cursor = (
            self.s._token("plan_cursor", **binding, after=after, expires=at + 300000)
            if more
            else None
        )
        return {
            "plans": result,
            "cursor": cursor,
            "complete": not more,
            "coverage": "current_authorized_plans_from_at_most_64_task_streams_per_page",
        }
