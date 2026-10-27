"""Bounded phone projections inside the existing canonical authorization transaction.

Only a trusted session adapter supplies ViewerSession. No browser credential, subject,
tenant or arbitrary operation is accepted here. Report rendering/export is unreachable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from memoryking.adapters.execution import Execution
from memoryking.adapters.knowledge_store import Knowledge
from memoryking.adapters.local_store import LocalStore, uid
from memoryking.adapters.report_store import Reports
from memoryking.adapters.retrieval import Retrieval
from memoryking.contracts.canonical import canonical_bytes, canonical_hash
from memoryking.contracts.viewer import validate_viewer
from memoryking.domain.local import LocalError, Released, temporal_valid
from memoryking.domain.viewer import VerifiedViewer


@dataclass(frozen=True)
class ViewerSession:
    subject: VerifiedViewer
    identity: str = field(repr=False)
    csrf: str = field(repr=False)
    expires_ms: int


def clipped(value: str, limit: int) -> tuple[str, bool]:
    raw = value.encode("utf-8")
    return raw[:limit].decode("utf-8", errors="ignore"), len(raw) > limit


class ViewerReads:
    def __init__(self, store: LocalStore, config: dict[str, Any]):
        self.s, self.db, self.config = store, store.db, config
        self.limits = config["limits"]

    def read(
        self,
        session: ViewerSession,
        route: str,
        project: str | None,
        params: dict[str, Any],
        *,
        deadline: float | None = None,
    ) -> Released:
        from memoryking.adapters.viewer_budget import request_budget

        with request_budget(self.s, deadline):
            return self._read(session, route, project, params)

    def _read(
        self, session: ViewerSession, route: str, project: str | None, params: dict[str, Any]
    ) -> Released:
        subject = session.subject
        if subject.tenant != self.config["tenant"] or subject.store != self.s.store_id:
            raise LocalError("unauthenticated")
        if subject.incarnation != self.s.incarnation:
            raise LocalError("context_changed")
        with self.s.transaction() as decision:
            at = decision.evaluated_ms
            if session.expires_ms <= at:
                raise LocalError("unauthenticated")
            who = self.s._decode(subject.credential, "credential")
            if any(
                who.get(k) != getattr(subject, k) for k in ("tenant", "principal", "incarnation")
            ):
                raise LocalError("unauthenticated")
            epochs = self.db.execute(
                "SELECT position,policy_epoch,purge_epoch FROM tenants WHERE tenant=?",
                (subject.tenant,),
            ).fetchone()
            binding = self.db.execute(
                "SELECT * FROM viewer_bindings WHERE tenant=? AND principal=?",
                (subject.tenant, subject.principal),
            ).fetchone()
            marker = {
                "user_id": subject.user_id,
                "generation": subject.generation,
                "policy_epoch": subject.policy_epoch,
            }
            if (
                binding is None
                or not binding["enabled"]
                or epochs[1] != subject.policy_epoch
                or binding["generation"] != subject.generation
                or binding["user_id"] != subject.user_id
                or who.get("viewer") != marker
            ):
                raise LocalError("context_changed")
            if project is not None:
                self.s._auth(subject.credential, "read", project, at)
            fresh = min(session.expires_ms, at + self.config["display_lifetime_seconds"] * 1000)
            fresh = self._freshness(who, project, at, fresh)
            identity = {
                "tenant": subject.tenant,
                "principal": subject.principal,
                "session": hashlib.sha256(session.identity.encode()).hexdigest(),
                "project": project,
                "epoch": epochs[1],
                "purge": epochs[2],
                "frontier": epochs[0],
                "generation": subject.generation,
            }
            request = {
                k: v
                for k, v in params.items()
                if k != "cursor" and not (route == "report_section" and k == "section")
            }
            query = canonical_hash({"route": route, "params": request})
            resume = None
            if params.get("cursor") is not None:
                resume = self._cursor(params["cursor"], identity, query, at)
            data, continuation, truncated, coverage = self._data(
                who, route, project, params, resume, at, session, identity, fresh
            )
            next_cursor = (
                None
                if continuation is None
                else self.s._token(
                    "viewer_cursor", **identity, query=query, until=fresh, continuation=continuation
                )
            )
            result = {
                "schema_version": 1,
                "binding": {
                    "store": self.s.store_id,
                    "tenant": subject.tenant,
                    "principal": subject.principal,
                    "project": project or "_session",
                    "incarnation": self.s.incarnation,
                    "authorization_epoch": epochs[1],
                    "session_generation": subject.generation,
                    "evaluated_ms": at,
                    "fresh_until_ms": fresh,
                    "observed_position": epochs[0],
                },
                "coverage": coverage,
                "truncated": truncated,
                "next_cursor": next_cursor,
                "data": data,
            }
            validate_viewer("ViewerResponse", result)
            raw = canonical_bytes(result)
            if len(raw) > self.limits["response_bytes"]:
                raise LocalError("response_limit")
            receipt = uid("rcpt")
            self.db.execute(
                "INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    who["tenant"],
                    receipt,
                    who["principal"],
                    self.s.incarnation,
                    at,
                    decision.deadline_ms,
                    epochs[1],
                    epochs[2],
                    canonical_hash(result),
                    "[]",
                ),
            )
            self.s.fault("after_validation")
        self.s._expose()
        return Released(raw, decision, self.s.clock)

    def _cursor(self, token: str, identity: dict[str, Any], query: str, at: int) -> Any:
        try:
            value = self.s._decode(token, "viewer_cursor")
            expected = {
                "kind": "viewer_cursor",
                "store": self.s.store_id,
                "incarnation": self.s.incarnation,
                **identity,
                "query": query,
            }
            if (
                {k: v for k, v in value.items() if k not in {"until", "continuation"}} != expected
                or type(value.get("until")) is not int
                or value["until"] <= at
            ):
                raise LocalError("invalid_cursor")
            return value["continuation"]
        except (LocalError, KeyError, TypeError) as exc:
            raise LocalError("invalid_cursor") from exc

    def _freshness(self, who: dict[str, Any], project: str | None, at: int, fresh: int) -> int:
        # Conservative tenant-wide lease cuts never disclose the hidden boundary's identity.
        for table in ("grants", "viewer_grants"):
            for column in ("starts", "expires"):
                row = self.db.execute(
                    f"SELECT MIN({column}) FROM {table} WHERE tenant=? AND {column}>?",
                    (who["tenant"], at),
                ).fetchone()
                if row[0] is not None:
                    fresh = min(fresh, row[0])
        for table in ("attestations", "code_validations"):
            row = self.db.execute(
                f"SELECT MIN(expires) FROM {table} WHERE tenant=? AND expires>?",
                (who["tenant"], at),
            ).fetchone()
            if row[0] is not None:
                fresh = min(fresh, row[0])
        for column in ("valid_start", "valid_end"):
            row = self.db.execute(
                f"SELECT MIN({column}) FROM replacements WHERE tenant=? AND {column}>?",
                (who["tenant"], at),
            ).fetchone()
            if row[0] is not None:
                fresh = min(fresh, row[0])
        for column in ("starts", "expires"):
            expression = f"CAST(json_extract(body,'$.{column}') AS BIGINT)"
            row = self.db.execute(
                f"SELECT MIN({expression}) FROM report_sources WHERE tenant=? AND project=? AND {expression}>?",
                (who["tenant"], project or "_session", at),
            ).fetchone()
            if row[0] is not None:
                fresh = min(fresh, row[0])
        rows = self.db.execute(
            "SELECT r.body FROM revisions r JOIN families f ON f.tenant=r.tenant AND f.id=r.family WHERE r.tenant=? AND f.project IN (?,?,?) AND r.body IS NOT NULL LIMIT 1025",
            (who["tenant"], project or "_session", "_tenant", "_workspace"),
        ).fetchall()
        if len(rows) > 1024:
            raise LocalError("service_unavailable")
        for row in rows:
            body = json.loads(row[0])
            for field_name in ("valid_start", "valid_end"):
                value = body.get(field_name)
                if value is not None and value > at:
                    fresh = min(fresh, value)
        return fresh

    def _policy(self, who: dict[str, Any], project: str, task: str | None, at: int) -> None:
        constraints = Knowledge(self.s).query(
            who,
            {"project": project, "task": task, "run": None, "bindings": [{}]},
            at,
            constraints=True,
        )
        if constraints["status"] != "complete":
            raise LocalError("unavailable")

    def _tasks(
        self,
        who: dict[str, Any],
        project: str,
        at: int,
        limit: int,
        after: str = "",
        task: str | None = None,
        identity: dict[str, Any] | None = None,
        fresh: int = 0,
    ) -> tuple[list[dict[str, Any]], bool]:
        snapshot = Execution(self.s).snapshot(
            who, project, at, viewer_after=after, viewer_task=task, viewer_limit=limit
        )
        if snapshot["status"] != "available":
            raise LocalError("unavailable")
        tasks = []
        for item in snapshot["tasks"]:
            detail = None
            if task is not None:
                row = Execution(self.s).w._checkpoint(
                    who,
                    {
                        "project": project,
                        "task": item["task"],
                        "run": None,
                        "checkpoint": item["checkpoint"],
                    },
                    at,
                )
                detail = json.loads(row["body"])
            original = {} if detail is None else {c["id"]: c for c in detail["acceptance"]}

            def evidence(dependencies: list[dict[str, str]]) -> list[dict[str, str]]:
                return [
                    {
                        "kind": d["kind"],
                        "handle": self.s._token(
                            "viewer_memory", **(identity or {}), until=fresh, id=d["id"]
                        ),
                    }
                    for d in dependencies[:8]
                ]

            # Restrict the phone projection: no operations, producer payloads or repository paths.
            tasks.append(
                {
                    **({"plan_id": item["plan_id"]} if "plan_id" in item else {}),
                    "id": item["task"],
                    "objective": item["objective"]
                    if detail is None
                    else clipped(detail["objective"], self.limits["detail_bytes"])[0],
                    "current_state": item["current_state"]
                    if detail is None
                    else clipped(detail["current_state"], self.limits["detail_bytes"])[0],
                    "checkpoint_revision": item["revision"],
                    "progress": {
                        k: item["progress"][k]
                        for k in ("passed", "failed", "pending", "waived", "unknown", "total")
                    },
                    "criteria": [
                        {
                            "id": c["id"],
                            "text": original.get(c["id"], c)["text"],
                            "evidence": evidence(c["evidence"]),
                            "evidence_truncated": len(c["evidence"]) > 8,
                            "state": c["effective"],
                            "evidence_available": c["evidence_available"],
                        }
                        for c in item["criteria"]
                    ],
                    "blockers": [
                        b["text"]
                        for b in (item["blockers"] if detail is None else detail["blockers"])
                    ],
                    "text_truncated": detail is None
                    or any(
                        clipped(detail[k], self.limits["detail_bytes"])[1]
                        for k in ("objective", "current_state")
                    ),
                    "coverage": "current_task_stream; bounded_text_and_evidence; criteria_from_current_evidence",
                }
            )
        if task is not None and not tasks:
            raise LocalError("unavailable")
        if not snapshot["scan_complete"] and not tasks:
            raise LocalError("service_unavailable")
        return tasks, not snapshot["scan_complete"]

    def _data(
        self,
        who: dict[str, Any],
        route: str,
        project: str | None,
        params: dict[str, Any],
        resume: Any,
        at: int,
        session: ViewerSession,
        identity: dict[str, Any],
        fresh: int,
    ) -> tuple[dict[str, Any], Any, bool, str]:
        if route == "session":
            rows = self.db.execute(
                "SELECT project,starts,expires FROM viewer_grants WHERE tenant=? AND principal=? ORDER BY project LIMIT 129",
                (who["tenant"], who["principal"]),
            ).fetchall()
            projects = []
            for row in rows[:128]:
                if temporal_valid(at, row["starts"], row["expires"]):
                    self.s._auth(session.subject.credential, "read", row["project"], at)
                    projects.append(row["project"])
            return (
                {
                    "kind": "session",
                    "projects": projects,
                    "csrf": session.csrf,
                    "poll_seconds": self.config["poll_seconds"],
                },
                None,
                len(rows) > 128,
                "current_explicit_project_grants_up_to_128",
            )
        if project is None:
            raise LocalError("invalid_request")
        task = (
            params.get("task")
            if route == "task"
            else params.get("plan")
            if route == "plan"
            else None
        )
        self._policy(who, project, task, at)
        limit = min(
            params.get("limit", 20 if route == "memories" else 25), self.limits["list_items"]
        )
        if route == "graph":
            return self._graph(who, project, at, identity, fresh)
        if route in {"plans", "plan"}:
            return self._plans(who, project, params, resume, at, identity, fresh, limit, route)
        if route in {"overview", "tasks", "task"}:
            tasks, more = self._tasks(who, project, at, limit, resume or "", task, identity, fresh)
            continuation = tasks[-1]["id"] if more and route == "tasks" else None
            return (
                {"kind": route, "tasks": tasks},
                continuation,
                more,
                "up_to_64_current_task_candidates; no_hidden_totals",
            )
        if route in {"memories", "memory"}:
            return self._memories(who, project, params, resume, at, identity, fresh, limit, route)
        if route.startswith("report"):
            return self._reports(who, project, params, resume, at, route, limit)
        raise LocalError("invalid_request")

    def _plans(
        self,
        who: dict[str, Any],
        project: str,
        params: dict[str, Any],
        resume: Any,
        at: int,
        identity: dict[str, Any],
        fresh: int,
        limit: int,
        route: str,
    ) -> tuple[dict[str, Any], Any, bool, str]:
        from memoryking.adapters.plans import Plans
        from memoryking.adapters.working import Working

        plans = Plans(Working(self.s))

        def summary(item: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
            p = item["progress"]
            return {
                **{k: item[k] for k in ("id", "revision", "updated_ms", "history_complete")},
                **{k: body[k] for k in ("title", "objective", "status")},
                "progress": {k: v for k, v in p.items() if k not in {"phases", "current_phase"}},
                "current_phase": p["current_phase"],
                "phase_count": len(p["phases"]),
            }

        coverage = "current_authorized_plan; equal_step_counts; skipped_separate; recorded_evidence_not_independent_certification"
        if route == "plans":
            result = plans.listing(who, {"project": project, "cursor": resume, "limit": limit}, at)
            return (
                {"kind": route, "plans": [summary(i, i) for i in result["plans"]]},
                result["cursor"],
                not result["complete"],
                coverage,
            )
        item = plans.get(who, {"project": project, "task": params["plan"], "revision": None}, at)
        body = item["plan"]
        phase = params.get("phase")
        if phase is not None and phase not in {p["id"] for p in body["phases"]}:
            raise LocalError("unavailable")
        steps = [s for s in body["steps"] if phase is None or s["phase"] == phase]
        offset = 0 if resume is None else resume
        if type(offset) is not int or not 0 <= offset <= len(steps):
            raise LocalError("invalid_cursor")
        page = steps[offset : offset + min(limit, 25)]

        def evidence(deps: list[dict[str, str]]) -> list[dict[str, str]]:
            return [
                {
                    "kind": d["kind"],
                    "handle": self.s._token("viewer_memory", **identity, until=fresh, id=d["id"]),
                }
                for d in deps[:4]
            ]

        detail = {
            **summary(item, body),
            **{k: body[k] for k in ("context", "closure_note")},
            "phases": item["progress"]["phases"],
            "steps": [
                {
                    **s,
                    "evidence": evidence(s["evidence"]),
                    "evidence_truncated": len(s["evidence"]) > 4,
                }
                for s in page
            ],
            "lessons": evidence(body["lessons"]),
            "lessons_truncated": len(body["lessons"]) > 4,
        }
        end = offset + len(page)
        more = end < len(steps)
        return {"kind": route, "plan": detail}, end if more else None, more, coverage

    def _graph(
        self,
        who: dict[str, Any],
        project: str,
        at: int,
        identity: dict[str, Any],
        fresh: int,
    ) -> tuple[dict[str, Any], Any, bool, str]:
        from memoryking.adapters.working import Working

        cap = self.limits["graph_nodes"]
        tasks, more = self._tasks(
            who, project, at, min(25, self.limits["list_items"]), identity=identity, fresh=fresh
        )
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, str]] = []
        seen_edges: set[tuple[str, str, str]] = set()
        references: list[tuple[str, str]] = []
        children: list[list[dict[str, Any]]] = []
        dependencies: list[tuple[str, str]] = []

        def label(text: str) -> str:
            value, truncated = clipped(text, 160)
            return (value.strip() or "Untitled") + ("…" if truncated else "")

        def link(source: str, target: str, kind: str) -> None:
            nonlocal more
            key = (source, target, kind)
            if key in seen_edges:
                return
            if len(edges) >= self.limits["graph_edges"]:
                more = True
                return
            seen_edges.add(key)
            edges.append({"source": source, "target": target, "kind": kind})

        # Reserve room for memory endpoints. Every shown task/plan gets a root
        # before round-robin children, so one large plan cannot hide all others.
        structural_cap = max(1, cap // 2)
        for item in tasks:
            if len(nodes) >= structural_cap:
                more = True
                break
            is_plan = "plan_id" in item
            parent = ("plan:" if is_plan else "task:") + item["id"]
            entries = []
            if is_plan:
                row = Working(self.s)._checkpoint(
                    who,
                    {"project": project, "task": item["id"], "run": None, "checkpoint": None},
                    at,
                )
                plan = json.loads(row["body"])["plan"]
                title = plan["title"]
                for ref in plan["lessons"][:8]:
                    references.append((parent, ref["id"]))
                more |= len(plan["lessons"]) > 8
                for step in plan["steps"]:
                    child = parent + ":" + step["id"]
                    entries.append(
                        {
                            "node": {
                                "id": child,
                                "label": label(step["description"]),
                                "kind": "step",
                                "task": item["id"],
                                "handle": None,
                                "phase": step["phase"],
                            },
                            "parent": parent,
                            "references": [r["id"] for r in step["evidence"][:8]],
                            "dependencies": [parent + ":" + d for d in step["depends_on"]],
                            "truncated": len(step["evidence"]) > 8,
                        }
                    )
            else:
                title = item["objective"]
                for criterion in item["criteria"]:
                    child = parent + ":" + criterion["id"]
                    entries.append(
                        {
                            "node": {
                                "id": child,
                                "label": label(criterion["text"]),
                                "kind": "criterion",
                                "task": item["id"],
                                "handle": None,
                            },
                            "parent": parent,
                            "references": [
                                self.s._decode(r["handle"], "viewer_memory")["id"]
                                for r in criterion["evidence"]
                            ],
                            "dependencies": [],
                            "truncated": criterion["evidence_truncated"],
                        }
                    )
            nodes.append(
                {
                    "id": parent,
                    "label": label(title),
                    "kind": "plan" if is_plan else "task",
                    "task": item["id"],
                    "handle": None,
                }
            )
            children.append(entries)
        for index in range(max((len(group) for group in children), default=0)):
            for group in children:
                if index >= len(group):
                    continue
                if len(nodes) >= structural_cap:
                    more = True
                    continue
                entry = group[index]
                node = entry["node"]
                nodes.append(node)
                link(entry["parent"], node["id"], "declares")
                references.extend((node["id"], identifier) for identifier in entry["references"])
                dependencies.extend((node["id"], target) for target in entry["dependencies"])
                more |= entry["truncated"]
        # Prefer recorded evidence over an unrelated old-observation backlog.
        # Canonical memory projection still rechecks visibility and accepted claims.
        preferred = list(dict.fromkeys(identifier for _, identifier in references))[:64]
        more |= len(set(identifier for _, identifier in references)) > 64
        memory_nodes: dict[str, str] = {}
        memories: dict[str, Any] = {"memories": []}
        memory_limit = min(self.limits["list_items"], cap - len(nodes))
        if memory_limit:
            memories, _, memory_more, _ = self._memories(
                who,
                project,
                {},
                None,
                at,
                identity,
                fresh,
                memory_limit,
                "memories",
                graph_sample=True,
                preferred=preferred,
            )
            more |= memory_more
        else:
            more = True
        for memory in memories["memories"]:
            identifier = self.s._decode(memory["handle"], "viewer_memory")["id"]
            node_id = "memory:" + canonical_hash({"session": identity["session"], "id": identifier})
            memory_nodes[identifier] = node_id
            nodes.append(
                {
                    "id": node_id,
                    "label": label(memory["text"]),
                    "kind": "claim" if memory["authority"] == "accepted" else "observation",
                    "task": None,
                    "handle": memory["handle"],
                }
            )
        shown = {n["id"] for n in nodes}
        for source, target in dependencies:
            if target in shown:
                link(source, target, "depends_on")
            else:
                more = True
        for source, identifier in references:
            if identifier in memory_nodes:
                link(source, memory_nodes[identifier], "uses")
            else:
                more = True
        for memory in memories["memories"]:
            if memory["authority"] != "accepted":
                continue
            identifier = self.s._decode(memory["handle"], "viewer_memory")["id"]
            for span in Knowledge(self.s)._evidence(who["tenant"], identifier):
                citation_target = memory_nodes.get(span["observation"])
                if citation_target is not None:
                    link(memory_nodes[identifier], citation_target, "cites")
        return (
            {"kind": "graph", "nodes": nodes, "edges": edges},
            None,
            more,
            "bounded_current_memories_claims_tasks_criteria_plans_steps; recorded_links_between_shown_nodes_only; no_hidden_totals_or_similarity",
        )

    def _memories(
        self,
        who: dict[str, Any],
        project: str,
        params: dict[str, Any],
        resume: Any,
        at: int,
        identity: dict[str, Any],
        fresh: int,
        limit: int,
        route: str,
        *,
        graph_sample: bool = False,
        preferred: list[str] | None = None,
    ) -> tuple[dict[str, Any], Any, bool, str]:
        wanted = None
        if route == "memory":
            try:
                handle = self.s._decode(params["handle"], "viewer_memory")
                expected = {
                    "kind": "viewer_memory",
                    "store": self.s.store_id,
                    "incarnation": self.s.incarnation,
                    **identity,
                }
                if {
                    k: v for k, v in handle.items() if k not in {"until", "id"}
                } != expected or handle["until"] <= at:
                    raise LocalError("unavailable")
                wanted = handle["id"]
            except (LocalError, KeyError, TypeError) as exc:
                raise LocalError("unavailable") from exc
        after = resume or {"position": 0, "id": ""}
        sql = (
            "SELECT * FROM observations WHERE tenant=? AND project=? AND deleted=0 AND position>=?"
        )
        values: tuple[Any, ...] = (who["tenant"], project, after["position"])
        if wanted is not None:
            sql += " AND id=?"
            values += (wanted,)
        sql += " AND (readers='null' OR EXISTS(SELECT 1 FROM json_each(observations.readers) WHERE value=?)) ORDER BY position,id LIMIT 65"
        values += (who["principal"],)
        rows = self.db.execute(sql, values).fetchall()
        if graph_sample and preferred:
            # At most 64 explicit IDs, scoped to this project. Visibility below
            # is identical to ordinary memory reads; never synthesize evidence.
            selected = self.db.execute(
                "SELECT * FROM observations WHERE tenant=? AND project=? AND deleted=0 AND id IN ("
                + ",".join("?" for _ in preferred[:64])
                + ")",
                (who["tenant"], project, *preferred[:64]),
            ).fetchall()
            rows = list({r["id"]: r for r in [*rows, *selected]}.values())
        candidates: list[tuple[int, str, Any, bool]] = [
            (r["position"], r["id"], r, False)
            for r in rows
            if (r["position"], r["id"]) > (after["position"], after["id"])
            and self.s._visible(who, r, at)
        ]
        knowledge = Knowledge(self.s).query(
            who,
            {"project": project, "task": None, "run": None, "bindings": [{}]},
            at,
            constraints=False,
        )
        if knowledge["status"] != "complete":
            raise LocalError("unavailable")
        for item in knowledge["items"]:
            if (wanted is None or item["revision"] == wanted) and (
                item["created_position"],
                item["revision"],
            ) > (after["position"], after["id"]):
                candidates.append((item["created_position"], item["revision"], item, True))
        candidates.sort(key=lambda candidate: candidate[:2])
        if graph_sample:
            # This is a bounded sample, not a paginated memory-list continuation.
            # Alternate kinds so an old observation backlog cannot hide every claim.
            observed_items = [c for c in candidates if not c[3]]
            accepted_items = [c for c in candidates if c[3]]
            candidates = []
            for i in range(max(len(observed_items), len(accepted_items))):
                for group in (observed_items, accepted_items):
                    if i < len(group):
                        candidates.append(group[i])
        if graph_sample and preferred:
            rank = {identifier: i for i, identifier in enumerate(preferred[:64])}
            candidates.sort(key=lambda c: rank.get(c[1], 64))
        result: list[dict[str, Any]] = []
        last, more = None, len(candidates) > 64 or len(rows) >= 65
        for position, identifier, row, derived in candidates[:64]:
            if len(result) == limit:
                more = True
                break
            last = {"position": position, "id": identifier}
            if derived:
                text = row["body"]["text"]
                event = self.db.execute(
                    "SELECT at_ms FROM events WHERE tenant=? AND position=?",
                    (who["tenant"], position),
                ).fetchone()
                observed = event[0]
                source, classification, authority = "accepted_knowledge", "restricted", "accepted"
            else:
                text = Retrieval(self.s)._blob(row).decode("utf-8")
                observed = row["observed_at_ms"]
                source, classification, authority = row["source"], row["classification"], "observed"
            if params.get("q", "").casefold() not in text.casefold():
                continue
            short, truncated = clipped(text, self.limits["detail_bytes"])
            result.append(
                {
                    "handle": self.s._token(
                        "viewer_memory", **identity, until=fresh, id=identifier
                    ),
                    "text": short,
                    "authority": authority,
                    "observed_ms": observed,
                    "evidence_available": True,
                    "truncated": truncated,
                    "source": source,
                    "classification": classification,
                }
            )
        if (wanted is not None and not result) or (more and last is None):
            raise LocalError("unavailable" if wanted is not None else "service_unavailable")
        return (
            {"kind": route, "memories": result},
            last if more else None,
            more,
            "up_to_64_current_authorized_observations_and_accepted_claims; unknown_applicability_blocks",
        )

    def _reports(
        self,
        who: dict[str, Any],
        project: str,
        params: dict[str, Any],
        resume: Any,
        at: int,
        route: str,
        limit: int,
    ) -> tuple[dict[str, Any], Any, bool, str]:
        reports = Reports(self.s)
        if route == "reports":
            result = reports._list(who, {"project": project, "limit": limit, "cursor": resume}, at)
            return (
                {"kind": route, "reports": result["reports"]},
                result["next_cursor"],
                result["next_cursor"] is not None,
                "current_whole_report_authority; bounded_catalog",
            )
        args = {"project": project, "report": params["report"], "revision": params["revision"]}
        row = reports._target(who, args, at)
        body = json.loads(row["body"])
        _, policy = reports._policy(who["tenant"], project, body["program"])
        if row["state"] != "published" or policy["channel"] == "kindle":
            raise LocalError("unavailable")
        if route == "report":
            states = self.db.execute(
                "SELECT state FROM report_deliveries WHERE tenant=? AND project=? AND report=? AND revision=?",
                (who["tenant"], project, args["report"], args["revision"]),
            ).fetchall()
            data = {
                k: body[k]
                for k in ("title", "program", "phase", "task", "technical_status", "review_status")
            }
            data.update(
                kind=route,
                report=args["report"],
                revision=args["revision"],
                sections=body["reader_pages"],
                channel=policy["channel"],
                delivery_states=sorted({s[0] for s in states}),
                device_delivery="unverified",
            )
            return data, None, False, "current_published_revision"
        if route == "report_section":
            # Outer cursor binds route/version and expected page; inner cursor enforces report ACL epochs.
            page = params["section"]
            inner = None
            if resume is not None:
                if resume["page"] != page:
                    raise LocalError("invalid_cursor")
                inner = resume["cursor"]
            result = reports._reader(who, row, {**args, "page": page, "cursor": inner})
            continuation = (
                None
                if result["next_cursor"] is None
                else {"page": page + 1, "cursor": result.pop("next_cursor")}
            )
            result.pop("next_cursor", None)
            return (
                {"kind": route, **result},
                continuation,
                continuation is not None,
                "lossless_ordered_canonical_block_fragments",
            )
        if route == "report_image":
            result = reports._image(
                who, row, {**args, "name": params["image"], "offset": params.get("offset", 0)}
            )
            return (
                {"kind": route, **result},
                None,
                result["next_offset"] is not None,
                "verified_local_raster_chunk",
            )
        raise LocalError("invalid_request")
