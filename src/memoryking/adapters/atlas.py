"""Bounded, current-authorized evidence graph; layout never creates authority."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from memoryking.contracts.canonical import canonical_bytes, canonical_hash
from memoryking.domain.briefing import relevance, terms
from memoryking.domain.local import Decision, LocalError


def query(
    retrieval: Any, who: dict[str, Any], args: dict[str, Any], decision: Decision, receipt: str
) -> tuple[bytes, dict[str, Any], list[dict[str, str]]]:
    s, db, k = retrieval.s, retrieval.db, retrieval.k
    at, tenant = decision.evaluated_ms, who["tenant"]
    scope: dict[str, Any] = {
        key: args[key]
        for key in ("project", "bindings", "task", "run", "valid_ms", "recorded_position")
        if key in args
    }
    state = db.execute("SELECT * FROM tenants WHERE tenant=?", (tenant,)).fetchone()
    recorded = args.get("recorded_position", state["position"])
    if recorded > state["position"]:
        raise LocalError("invalid_recorded_position")
    # No search, degree or aggregate is computed from an unauthorized graph.
    # The scan is finite; these counts only describe the loaded authorized window.
    rows = db.execute(
        "SELECT o.* FROM observations o WHERE o.tenant=? AND o.project=? AND o.deleted=0 AND o.position<=? AND (o.readers='null' OR EXISTS(SELECT 1 FROM json_each(o.readers) WHERE value=?)) ORDER BY o.position,o.id LIMIT 1001",
        (tenant, args["project"], recorded, who["principal"]),
    ).fetchall()
    nodes: dict[str, dict[str, Any]] = {}
    dependencies: dict[str, list[dict[str, str]]] = {}
    edges: list[dict[str, Any]] = []
    limits: set[str] = {"observation_scan"} if len(rows) > 1000 else set()
    for row in rows[:1000]:
        if not s._visible(who, row, at):
            continue
        raw = retrieval._blob(row)
        nodes[row["id"]] = {
            "id": row["id"],
            "type": "observation",
            "label": raw.decode()[:256],
            "authority": "observed_episode",
            "content_hash": row["blob"],
            "source": row["source"],
            "position": row["position"],
            "anchors": [],
        }
        dependencies[row["id"]] = [{"kind": "observation", "id": row["id"]}]
    # Exact names the loaded observations share (paths, symbols, tickets, hosts, errors)
    # link them, A-MEM style, without any model: an anchor held by two to eight of the
    # loaded records is rare enough to mean something; a commoner one is just a word.
    by_anchor: dict[str, list[str]] = {}
    if nodes:
        loaded = list(nodes)
        for start in range(0, len(loaded), 500):
            chunk = loaded[start : start + 500]
            for item in db.execute(
                "SELECT id,anchor,kind FROM anchors WHERE tenant=? AND id IN ("
                + ",".join("?" for _ in chunk)
                + ") ORDER BY id,anchor",
                (tenant, *chunk),
            ).fetchall():
                if len(nodes[item["id"]]["anchors"]) < 16:
                    nodes[item["id"]]["anchors"].append(
                        {"kind": item["kind"], "anchor": item["anchor"]}
                    )
                by_anchor.setdefault(item["anchor"], []).append(item["id"])

    anchor_links = 0

    def edge(source: str, target: str, kind: str, provenance: dict[str, Any]) -> None:
        if source in nodes and target in nodes:
            if len(edges) >= 6000:
                limits.add("edge_scan")
                return
            item = {"source": source, "target": target, "type": kind, "provenance": provenance}
            edges.append({"id": canonical_hash(item), **item})

    for anchor, holders in sorted(by_anchor.items()):
        holders = sorted(set(holders))
        if not 2 <= len(holders) <= 8:
            continue
        for left, right in zip(holders, holders[1:], strict=False):
            if anchor_links >= 512:
                limits.add("anchor_links")
                break
            edge(left, right, "shares_anchor", {"kind": "shared_anchor", "anchor": anchor})
            anchor_links += 1

    attestation_count = 0
    # A reviewer also sees proposed, rejected, deprecated and quarantined claims, in the
    # same diagnostic mode the review queue uses; a plain reader sees accepted ones only.
    try:
        s._subject_auth(
            s._token("credential", tenant=tenant, principal=who["principal"]),
            "review",
            args["project"],
            at,
        )
        reviewer = True
    except LocalError:
        reviewer = False
    for constraints in (False, True):
        result = k.query(
            who,
            {**scope, "diagnostic": True} if reviewer and not constraints else scope,
            at,
            constraints=constraints,
            attestation_limit=33,
        )
        if result["status"] != "complete":
            limits.add("claim_resolution_incomplete")
        for item in result["items"]:
            ident, body = item["revision"], item["body"]
            if "created_position" not in item:
                # A claim whose selector the store cannot evaluate has no recorded
                # position in this result; it is listed in the review queue instead.
                limits.add("unsupported_selector_claims_omitted")
                continue
            nodes[ident] = {
                "id": ident,
                "type": "claim",
                "label": body["text"][:256],
                "authority": item["authority"],
                "content_hash": item["body_hash"],
                "family": item["family"],
                "state": item["state"],
                "quarantined": bool(item.get("quarantined", False)),
                "applicability": item["applicability"],
                "position": item["created_position"],
            }
            dependencies[ident] = retrieval._dependencies([retrieval._claim(item)])
            for span in body["evidence"]:
                edge(
                    span["observation"],
                    ident,
                    "supports",
                    {"kind": "recorded_evidence_span", **span},
                )
            for att in item["attestations"][:32]:
                if attestation_count >= 256:
                    limits.add("attestation_scan")
                    break
                attestation_count += 1
                aid = "attestation:" + ident + ":" + str(att["position"])
                nodes[aid] = {
                    "id": aid,
                    "type": "attestation",
                    "label": att["statement"][:256],
                    "authority": "actor_attestation",
                    "actor": att["actor"],
                    "expires_ms": att["expires"],
                    "position": att["position"],
                }
                dependencies[aid] = dependencies[ident]
                edge(
                    ident, aid, "verified_by", {"kind": "attestation", "position": att["position"]}
                )
            if len(item["attestations"]) > 32:
                limits.add("attestation_limit")
        for left, right in result.get("conflicts", []):
            edge(left, right, "contradicts", {"kind": "eligible_claim_conflict"})

    # P10 adds actual procedure/checkpoint relationships. Historical requests do
    # not mix present procedure activation into an older recorded frontier.
    if not {"recorded_position", "valid_ms"}.intersection(args):
        from memoryking.adapters.experience import Experience

        if len(args["bindings"]) <= 32:
            procedures = Experience(s).query(who, scope, at)
            if procedures["status"] != "complete":
                limits.add("procedure_resolution_incomplete")
            for item in procedures["items"]:
                ident, body = item["revision"], item["body"]
                nodes[ident] = {
                    "id": ident,
                    "type": "procedure",
                    "label": body["text"][:256],
                    "authority": item["authority"],
                    "state": item["effective_state"],
                    "position": item["created_position"],
                    "content_hash": item["body_hash"],
                    "execution_privileges": False,
                }
                dependencies[ident] = retrieval._dependencies([retrieval._claim(item)])
                activation = item["activation"]
                if activation is not None:
                    dependencies[ident].append(
                        {
                            "kind": "procedure_activation",
                            "id": f"{ident}|{activation['position']}|{item['family_version']}",
                        }
                    )
                for span in body["evidence"]:
                    edge(
                        span["observation"],
                        ident,
                        "supports",
                        {"kind": "recorded_evidence_span", **span},
                    )
        else:
            limits.add("procedure_binding_limit")
    else:
        limits.add("historical_procedures_omitted")

    from memoryking.adapters.execution import Execution

    execution = Execution(s).snapshot(who, args["project"], at, args.get("recorded_position"))
    if execution["status"] != "available":
        limits.add("checkpoint_projection_unavailable")
    for task in execution["tasks"]:
        if (
            args.get("task", task["task"]) != task["task"]
            or args.get("run", task["run"]) != task["run"]
        ):
            continue
        ident = task["checkpoint"]
        row = db.execute(
            "SELECT body,dependencies FROM checkpoints WHERE tenant=? AND id=?", (tenant, ident)
        ).fetchone()
        body = json.loads(row["body"]) if row is not None and row["body"] else {}
        plan = body.get("plan")
        nodes[ident] = {
            "id": ident,
            "type": "plan" if plan else "checkpoint",
            "label": (plan["title"] if plan else task["objective"])[:256],
            "authority": "recorded_plan" if plan else "producer_checkpoint",
            "position": task["source_position"],
            "revision": task["revision"],
            "content_hash": task["body_hash"],
            **({"status": plan["status"]} if plan else {}),
        }
        dependencies[ident] = [{"kind": "checkpoint", "id": ident}]
        if plan:
            # Steps are the plan's own structure: declared by the plan, ordered by their
            # prerequisites, and tied to the observations they cite as proof. A step
            # shares the plan's receipt dependency; it never becomes evidence itself.
            for step in plan["steps"]:
                sid = "step:" + ident + ":" + step["id"]
                nodes[sid] = {
                    "id": sid,
                    "type": "step",
                    "label": step["description"][:256],
                    "authority": "recorded_plan_step",
                    "position": task["source_position"],
                    "status": step["status"],
                    "phase": step["phase"],
                }
                dependencies[sid] = dependencies[ident]
            for step in plan["steps"]:
                sid = "step:" + ident + ":" + step["id"]
                edge(ident, sid, "declares", {"kind": "plan_step", "checkpoint": ident})
                for prerequisite in step["depends_on"]:
                    edge(
                        sid,
                        "step:" + ident + ":" + prerequisite,
                        "depends_on",
                        {"kind": "plan_step_prerequisite", "checkpoint": ident},
                    )
                for proof in step["evidence"]:
                    edge(
                        sid,
                        proof["id"],
                        "cites",
                        {
                            "kind": "plan_step_evidence",
                            "checkpoint": ident,
                            "status": step["status"],
                        },
                    )
            for lesson in plan["lessons"]:
                edge(ident, lesson["id"], "cites", {"kind": "plan_lesson", "checkpoint": ident})
        tid = "task:" + args["project"] + ":" + task["task"]
        nodes[tid] = {
            "id": tid,
            "type": "task",
            "label": task["task"],
            "authority": "recorded_task_identity",
            "position": task["source_position"],
        }
        dependencies.setdefault(tid, []).extend(dependencies[ident])
        edge(ident, tid, "documented_in", {"kind": "checkpoint_scope", "checkpoint": ident})
        if task["run"] is not None:
            rid = "run:" + canonical_hash([args["project"], task["task"], task["run"]])
            nodes[rid] = {
                "id": rid,
                "type": "run",
                "label": task["run"],
                "authority": "recorded_run_identity",
                "position": task["source_position"],
            }
            dependencies[rid] = dependencies[ident]
            edge(rid, tid, "documented_in", {"kind": "checkpoint_scope", "checkpoint": ident})
            edge(ident, rid, "documented_in", {"kind": "checkpoint_scope", "checkpoint": ident})
        for dep in json.loads(row["dependencies"]) if row is not None else []:
            edge(
                ident,
                dep["id"],
                "depends_on",
                {
                    "kind": "checkpoint_dependency",
                    "checkpoint": ident,
                    "dependency_kind": dep["kind"],
                },
            )
    limits.add("checkpoint_window")
    # Size by citation: how many of the caller's own retained briefings, fetches and
    # reruns depended on the record. Atlas pages are excluded, so looking never cites.
    cited = retrieval.citations(who, args["project"], at)
    for ident, node in nodes.items():
        node["citations"] = cited.get(ident, 0)
    wanted_types = set(
        args.get(
            "types",
            [
                "observation",
                "claim",
                "attestation",
                "procedure",
                "checkpoint",
                "plan",
                "step",
                "task",
                "run",
            ],
        )
    )
    visible = {i for i, node in nodes.items() if node["type"] in wanted_types}
    search = terms(args.get("query", ""))
    if search:
        visible = {i for i in visible if relevance(search, nodes[i]["label"])}
    if args.get("focus"):
        seeds = set(args["focus"]) & visible
        visible = seeds | {
            i
            for e in edges
            if e["source"] in seeds or e["target"] in seeds
            for i in (e["source"], e["target"])
            if i in visible
        }
    ordered = sorted(visible, key=lambda i: (nodes[i]["position"], i))
    visible_edges = sorted(
        (e for e in edges if e["source"] in visible and e["target"] in visible),
        key=lambda e: e["id"],
    )
    binding = {
        "store": s.store_id,
        "tenant": tenant,
        "principal": who["principal"],
        "project": args["project"],
        "incarnation": s.incarnation,
        "authorization_epoch": state["policy_epoch"],
        "purge_epoch": state["purge_epoch"],
        "delegation_chain": who.get("delegation_chain", []),
    }
    # Re-evaluate membership on every page, including temporal expiry. A changed
    # authorized view invalidates offsets; old cursors cannot stand in for grants.
    fingerprint = hashlib.sha256(
        canonical_bytes(
            {
                "binding": binding,
                "source": state["position"],
                "request": {key: value for key, value in args.items() if key != "cursor"},
            }
        )
    )
    for item in [*(nodes[i] for i in ordered), *visible_edges]:
        fingerprint.update(canonical_bytes(item))
        fingerprint.update(b"\n")
    view_hash = fingerprint.hexdigest()
    offset = 0
    if args.get("cursor"):
        cursor = s._decode(args["cursor"], "atlas")
        if cursor.get("view") != view_hash or at >= cursor.get("expires", 0):
            raise LocalError("atlas_resync_required")
        offset = cursor["offset"]
    selected = ordered[offset : offset + args.get("max_nodes", 200)]
    edge_limit = args.get("max_edges", 400)

    def materialize() -> dict[str, Any]:
        page = set(selected)
        permitted_edges = [e for e in visible_edges if e["source"] in page and e["target"] in page]
        truncated = set(limits)
        if len(permitted_edges) > edge_limit:
            truncated.add("edge_limit")
        if offset or offset + len(selected) < len(ordered):
            truncated.add("node_page")
        if any((e["source"] in page) != (e["target"] in page) for e in visible_edges):
            truncated.add("edges_outside_page")
        next_offset = offset + len(selected)
        return {
            "schema": "atlas-v1",
            "status": "partial",
            "label_scope": "first_256_characters_inspection_only",
            "receipt": receipt,
            "evaluated_ms": at,
            "deadline_ms": decision.deadline_ms,
            "binding": binding,
            "nodes": [nodes[i] for i in selected],
            "edges": permitted_edges[:edge_limit],
            "list": [
                {"id": i, "label": nodes[i]["label"], "type": nodes[i]["type"]} for i in selected
            ],
            "counts": {
                kind: sum(nodes[i]["type"] == kind for i in selected)
                for kind in sorted(wanted_types)
            },
            "count_scope": "returned_page_only",
            "coverage": "bounded_current_authorized_window",
            "truncation": sorted(truncated),
            "source_position": state["position"],
            "next_cursor": s._token("atlas", view=view_hash, offset=next_offset, expires=at + 60000)
            if next_offset < len(ordered)
            else None,
            "similarity": "unavailable",
            "layout_distance": "not_confidence",
        }

    response = materialize()
    while len(json.dumps(response, ensure_ascii=False).encode()) > 196608 and selected:
        limits.add("response_bytes")
        selected.pop()
        response = materialize()
    deps = {(d["kind"], d["id"]) for i in selected for d in dependencies[i]}
    raw = canonical_bytes(response)
    return (
        raw,
        {
            "kind": "atlas",
            "status": "partial",
            "usage": {"serialized_bytes": len(raw), "provider_usage": "not_used"},
            "coverage": response["coverage"],
            "truncation": response["truncation"],
            "serialized_bytes": len(raw),
        },
        [{"kind": kind, "id": ident} for kind, ident in sorted(deps)],
    )
