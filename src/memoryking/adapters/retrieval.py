"""Local lexical compilation, immutable rendered artifacts and authorized inspection."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator

from memoryking.adapters.dense_search import search as dense_search
from memoryking.adapters.knowledge_store import Knowledge
from memoryking.adapters.local_files import private_file
from memoryking.adapters.local_store import (
    ARTIFACT_BYTES_LIMIT,
    ARTIFACT_LIMIT,
    RETRY_MS,
    uid,
)
from memoryking.adapters.token_count import counter
from memoryking.application.store_core import StoreCore
from memoryking.contracts.canonical import canonical_bytes, canonical_hash
from memoryking.domain.anchors import anchors_in
from memoryking.domain.briefing import (
    COMPILER,
    CONFIGURATION,
    QUERY_TERM_LIMIT,
    counted,
    pack,
    relevance,
    term_window,
    terms,
)
from memoryking.domain.briefing_text import textualize
from memoryking.domain.local import Decision, LocalError, Released
from memoryking.domain.selectors import bindings

# Salience: a bounded, documented nudge from use, never a rewrite of the record.
SALIENCE_WEIGHT = 100_000  # at most a tenth of a full lexical score, in millionths
SALIENCE_WINDOW_MS = 90 * 86_400_000  # deliveries older than this do not count
SALIENCE_HALF_LIFE_MS = 30 * 86_400_000  # use fades by half every thirty days
ARCHIVE_AFTER_MS = 180 * 86_400_000  # never-used records older than this rank last
ARCHIVAL_PENALTY = 4_000_000  # below any reachable score; archival stays searchable
VALIDATOR = Draft202012Validator(
    json.loads(Path(__file__).with_name("retrieval.schema.json").read_text())
)


class Retrieval:
    def __init__(self, store: StoreCore):
        self.s, self.db, self.k = store, store.db, Knowledge(store)
        if not hasattr(store, "retrieval_cache"):
            store.retrieval_cache = {}

    def execute(self, credential: str, command: dict[str, Any]) -> Released:
        if len(canonical_bytes(command)) > 131072 or not VALIDATOR.is_valid(command):
            raise LocalError("invalid_request")
        if command["incarnation"] != self.s.incarnation:
            raise LocalError("history_changed")
        action, args = command["action"], command["args"]
        access_id = uid("rcpt")
        with self.s.transaction() as decision:
            at = decision.evaluated_ms
            who = self.s._auth(credential, "read", args["project"], at)
            self._expire(at)
            predecessor = None
            if action == "rerun":
                previous = self.db.execute(
                    "SELECT incarnation FROM rendered_artifacts WHERE tenant=? AND principal=? AND project=? AND id=?",
                    (who["tenant"], who["principal"], args["project"], args["predecessor"]),
                ).fetchone()
                if previous is None:
                    raise LocalError("replay_missing")
                if previous["incarnation"] != self.s.incarnation:
                    raise LocalError("history_changed")
                # Linking an owned tombstone does not read the expired/purged body.
                # All new inputs are explicit and undergo fresh eligibility below.
                predecessor = args["predecessor"]
                if args["brief"]["project"] != args["project"]:
                    raise LocalError("invalid_request")
                args = args["brief"]
                action = "brief"
            deps: list[dict[str, str]] = []
            if action in {"brief", "fetch", "atlas"}:
                if action == "atlas":
                    from memoryking.adapters.atlas import query

                    raw, diag, deps = query(self, who, args, decision, access_id)
                elif action == "brief":
                    raw, diag, deps = self._brief(who, args, decision, access_id)
                    raw, diag = self.textual(args, "brief", raw, diag)
                else:
                    raw, diag, deps = self._fetch(who, args, decision, access_id)
                diag["predecessor"] = predecessor
                self._retain(who, args, access_id, raw, diag, deps, at)
            elif action in {"replay", "receipt"}:
                row = self._artifact(who, args["project"], args["receipt"], at)
                deps = json.loads(row["dependencies"])
                if action == "replay":
                    raw = bytes(row["body"])
                else:
                    raw = canonical_bytes(
                        {
                            "receipt": row["id"],
                            "diagnostics": json.loads(row["diagnostics"]),
                            "rendered_hash": row["body_hash"],
                            "expires_ms": row["expires"],
                            "access_receipt": access_id,
                            "evaluated_ms": at,
                            "deadline_ms": decision.deadline_ms,
                            "replay": "available",
                        }
                    )
            else:
                raw = canonical_bytes(
                    {
                        "items": self.list_receipts(who, args["project"], at),
                        "access_receipt": access_id,
                    }
                )
            epochs = self.db.execute(
                "SELECT policy_epoch,purge_epoch FROM tenants WHERE tenant=?", (who["tenant"],)
            ).fetchone()
            self.db.execute(
                "INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    who["tenant"],
                    access_id,
                    who["principal"],
                    self.s.incarnation,
                    at,
                    decision.deadline_ms,
                    *epochs,
                    hashlib.sha256(raw).hexdigest(),
                    json.dumps([d["id"] for d in deps if d["kind"] == "observation"]),
                ),
            )
            self.s.fault("after_validation")
        self.s._expose()
        return Released(raw, decision, self.s.clock, access_id)

    def _auto_dense(self, who: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        """Embed the query with the daemon's model when the project has a partition for it.

        Only when the caller sent no `dense` request: an explicit request, including an
        explicit vector, is never overridden. Without a model or a matching experimental
        partition the briefing stays lexical, exactly as before.
        """
        embedder = self.s.embedder
        if embedder is None or "dense" in args:
            return args
        row = self.db.execute(
            "SELECT 1 FROM embedding_partitions WHERE tenant=? AND project=? AND fingerprint=? AND state='experimental' AND incarnation=?",
            (who["tenant"], args["project"], embedder.fingerprint, self.s.incarnation),
        ).fetchone()
        if row is None:
            return args
        try:
            batch = embedder.embed([args["query"]])
        except Exception:  # a model failure degrades to lexical retrieval, never a refusal
            return args
        return {
            **args,
            "dense": {
                "fingerprint": embedder.fingerprint,
                "query_hash": canonical_hash(args["query"]),
                "vector": batch.vectors[0],
            },
        }

    def textual(
        self, args: dict[str, Any], kind: str, raw: bytes, diag: dict[str, Any]
    ) -> tuple[bytes, dict[str, Any]]:
        """Render the packet as cited Markdown when the caller asked for text."""
        if args.get("format") != "text":
            return raw, diag
        count, _ = counter(args["tokenizer"])
        envelope = textualize(json.loads(raw), kind, count)
        raw = canonical_bytes(envelope)
        diag = {
            **diag,
            "format": "text",
            "usage": {
                **diag.get("usage", {}),
                "output_tokens": envelope["usage"]["output_tokens"],
                "serialized_bytes": len(raw),
            },
        }
        return raw, diag

    def _make_room(self, who: dict[str, Any], incoming: int) -> int:
        """Evict the oldest retained artifacts at capacity, the caller's own first.

        Replay of an evicted receipt reports replay_expired. A caller looping on brief
        therefore consumes its own retention before anyone else's.
        """
        if incoming > ARTIFACT_BYTES_LIMIT:
            raise LocalError("retention_capacity")
        evicted = 0
        while True:
            count, size = self.db.execute(
                "SELECT count(*),coalesce(sum(length(body)),0) FROM rendered_artifacts WHERE tenant=? AND state='retained'",
                (who["tenant"],),
            ).fetchone()
            if count < ARTIFACT_LIMIT and size + incoming <= ARTIFACT_BYTES_LIMIT:
                return evicted
            oldest = (
                self.db.execute(
                    "SELECT id FROM rendered_artifacts WHERE tenant=? AND principal=? AND state='retained' ORDER BY created,id LIMIT 1",
                    (who["tenant"], who["principal"]),
                ).fetchone()
                or self.db.execute(
                    "SELECT id FROM rendered_artifacts WHERE tenant=? AND state='retained' ORDER BY created,id LIMIT 1",
                    (who["tenant"],),
                ).fetchone()
            )
            if oldest is None:
                raise LocalError("retention_capacity")
            self.db.execute(
                "UPDATE rendered_artifacts SET state='evicted',body=NULL,request_json=NULL,diagnostics=NULL,dependencies='[]' WHERE tenant=? AND id=?",
                (who["tenant"], oldest["id"]),
            )
            evicted += 1

    def _expire(self, at: int) -> None:
        self.db.execute(
            "UPDATE rendered_artifacts SET state='expired',body=NULL,request_json=NULL,diagnostics=NULL,dependencies='[]' WHERE state='retained' AND expires<=?",
            (at,),
        )

    def _artifact(self, who: dict[str, Any], project: str, receipt: str, at: int) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT * FROM rendered_artifacts WHERE tenant=? AND id=? AND principal=? AND project=?",
            (who["tenant"], receipt, who["principal"], project),
        ).fetchone()
        if row is None:
            raise LocalError("replay_missing")
        if row["incarnation"] != self.s.incarnation:
            raise LocalError("history_changed")
        if row["state"] == "purged":
            raise LocalError("replay_purged")
        if row["state"] in {"expired", "evicted"} or at >= row["expires"]:
            raise LocalError("replay_expired")
        if row["body"] is None or hashlib.sha256(row["body"]).hexdigest() != row["body_hash"]:
            raise LocalError("replay_integrity_failure")
        for dep in json.loads(row["dependencies"]):
            self._dependency(who, dep, at)
        return cast(sqlite3.Row, row)

    def _dependency(self, who: dict[str, Any], dep: dict[str, str], at: int) -> None:
        if dep["kind"] == "observation":
            row = self.db.execute(
                "SELECT * FROM observations WHERE tenant=? AND id=?", (who["tenant"], dep["id"])
            ).fetchone()
            if row is None or not self.s._visible(who, row, at):
                raise LocalError("replay_denied")
        elif dep["kind"] == "experience_record":
            from memoryking.adapters.experience_records import ExperienceRecords

            ExperienceRecords(self.s).verify_dependency(who, dep["id"], at)
        elif dep["kind"] == "procedure_activation":
            from memoryking.adapters.experience import Experience

            Experience(self.s).verify_dependency(who, dep["id"], at)
        elif dep["kind"] == "code_validation":
            from memoryking.adapters.repository_store import Repository

            Repository(self.s).verify_proof(who, dep["id"], at)
        elif dep["kind"] == "handoff_view":
            from memoryking.adapters.handoff import Handoff

            Handoff(self.s).verify_dependency(who, dep["id"], at)
        elif dep["kind"] == "checkpoint":
            row = self.db.execute(
                "SELECT * FROM checkpoints WHERE tenant=? AND id=?", (who["tenant"], dep["id"])
            ).fetchone()
            if (
                row is None
                or row["incarnation"] != self.s.incarnation
                or row["state"] != "available"
                or row["body"] is None
                or who["principal"] not in json.loads(row["readers"])
                or hashlib.sha256(row["body"].encode()).hexdigest() != row["body_hash"]
            ):
                raise LocalError("replay_denied")
            token = self.s._token("credential", tenant=who["tenant"], principal=who["principal"])
            self.s._subject_auth(token, "read", row["project"], at)
            for source in json.loads(row["dependencies"]):
                self._dependency(who, source, at)
        else:
            revision = self.k._revision(who["tenant"], dep["id"])
            if (
                not self.k._available(who, revision, at)
                or self.k._state(who["tenant"], dep["id"], 9007199254740991)[1]
            ):
                raise LocalError("replay_denied")

    def list_receipts(self, who: dict[str, Any], project: str, at: int) -> list[dict[str, Any]]:
        items = []
        for candidate in self.db.execute(
            "SELECT id FROM rendered_artifacts WHERE tenant=? AND principal=? AND project=? AND incarnation=? AND state='retained' AND expires>? ORDER BY created DESC,id DESC LIMIT 32",
            (who["tenant"], who["principal"], project, self.s.incarnation, at),
        ).fetchall():
            try:
                row = self._artifact(who, project, candidate["id"], at)
            except LocalError:
                continue
            diag = json.loads(row["diagnostics"])
            items.append(
                {
                    "receipt": row["id"],
                    "created_ms": row["created"],
                    "expires_ms": row["expires"],
                    "rendered_hash": row["body_hash"],
                    "status": diag["status"],
                    "usage": diag["usage"],
                    "cache": diag.get("cache", "not_applicable"),
                }
            )
        return items

    def citations(self, who: dict[str, Any], project: str, at: int) -> dict[str, int]:
        """How many of the caller's retained briefings, fetches and reruns depend on each record.

        Atlas pages depend on every node they show, so they are not citations. Only the
        caller's own artifacts count: a receipt is private to the principal that holds it.
        """
        counts: dict[str, int] = {}
        for row in self.db.execute(
            "SELECT diagnostics,dependencies FROM rendered_artifacts WHERE tenant=? AND principal=? AND project=? AND incarnation=? AND state='retained' AND expires>?",
            (who["tenant"], who["principal"], project, self.s.incarnation, at),
        ).fetchall():
            if json.loads(row["diagnostics"]).get("kind") == "atlas":
                continue
            for dep in json.loads(row["dependencies"]):
                counts[dep["id"]] = counts.get(dep["id"], 0) + 1
        return counts

    def list_citations(
        self, who: dict[str, Any], project: str, node: str, at: int
    ) -> list[dict[str, Any]]:
        """The caller's retained receipts that depend on one record, newest first.

        Each receipt is re-authorized exactly as `receipts` does, so a record whose
        evidence the caller may no longer read yields no receipt.
        """
        if not re.fullmatch(r"[A-Za-z0-9_.:|-]{1,256}", node):
            raise LocalError("invalid_request")
        items = []
        for candidate in self.db.execute(
            "SELECT id,diagnostics,dependencies FROM rendered_artifacts WHERE tenant=? AND principal=? AND project=? AND incarnation=? AND state='retained' AND expires>? ORDER BY created DESC,id DESC",
            (who["tenant"], who["principal"], project, self.s.incarnation, at),
        ).fetchall():
            if json.loads(candidate["diagnostics"]).get("kind") == "atlas":
                continue
            kinds = [d["kind"] for d in json.loads(candidate["dependencies"]) if d["id"] == node]
            if not kinds:
                continue
            try:
                row = self._artifact(who, project, candidate["id"], at)
            except LocalError:
                continue
            diag = json.loads(row["diagnostics"])
            items.append(
                {
                    "receipt": row["id"],
                    "created_ms": row["created"],
                    "expires_ms": row["expires"],
                    "rendered_hash": row["body_hash"],
                    "status": diag["status"],
                    "usage": diag["usage"],
                    "dependency_kind": kinds[0],
                    "format": diag.get("format", "json"),
                }
            )
            if len(items) >= 32:
                break
        return items

    def _retain(
        self,
        who: dict[str, Any],
        args: dict[str, Any],
        receipt: str,
        raw: bytes,
        diag: dict[str, Any],
        deps: list[dict[str, str]],
        at: int,
    ) -> None:
        self._make_room(who, len(raw))
        if len(canonical_bytes(diag)) > 65536:
            raise LocalError("diagnostic_limit")
        self.db.execute(
            "INSERT INTO rendered_artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                who["tenant"],
                receipt,
                who["principal"],
                args["project"],
                self.s.incarnation,
                at,
                at + RETRY_MS,
                "retained",
                raw,
                hashlib.sha256(raw).hexdigest(),
                json.dumps(args),
                json.dumps(diag),
                json.dumps(deps),
            ),
        )

    def _base(
        self, args: dict[str, Any], decision: Decision, receipt: str, usage: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "status": "partial",
            "receipt": receipt,
            "incarnation": self.s.incarnation,
            "evaluated_ms": decision.evaluated_ms,
            "deadline_ms": decision.deadline_ms,
            "usage": {
                **usage,
                "max_output_tokens": args["max_output_tokens"],
                "budget_compliant": True,
                "provider_usage": "unknown",
            },
            "warnings": ["untrusted_evidence", "host_and_protocol_tokens_excluded"]
            + (["token_estimate_no_exact_guarantee"] if usage["counting"] != "exact" else []),
            "constraints": [],
            "conflicts": [],
            "guidance": [],
        }

    def _brief(
        self,
        who: dict[str, Any],
        args: dict[str, Any],
        decision: Decision,
        receipt: str,
        *,
        ranking_override: dict[str, Any] | None = None,
    ) -> tuple[bytes, dict[str, Any], list[dict[str, str]]]:
        at, tenant = decision.evaluated_ms, who["tenant"]
        start = self.s.clock.monotonic_ms()
        bindings(args["bindings"])
        count, usage = counter(args["tokenizer"])
        from memoryking.adapters.ranking import Ranking

        tenant_row = self.db.execute("SELECT * FROM tenants WHERE tenant=?", (tenant,)).fetchone()
        source = tenant_row["position"]
        if "as_of_ms" in args:
            # One instant stands for both clocks: the history frontier as it was then
            # (the last event recorded by that wall time) and the claim validity time.
            if "recorded_position" in args or "valid_ms" in args:
                raise LocalError("invalid_request")
            frontier = self.db.execute(
                "SELECT coalesce(max(position),0) FROM events WHERE tenant=? AND at_ms<=?",
                (tenant, args["as_of_ms"]),
            ).fetchone()[0]
            args = {**args, "recorded_position": int(frontier), "valid_ms": args["as_of_ms"]}
        ranking: dict[str, Any] = (
            ranking_override
            if ranking_override is not None
            else {"state": "historical_override_disabled", "adjustments": {}}
            if "recorded_position" in args or "valid_ms" in args
            else Ranking(self.s).active(who, args["project"], at)
        )
        consistency = args.get("consistency", {})
        target_position = source
        if token := consistency.get("at_least_watermark"):
            target = self.s._decode(token, "watermark")
            if target["tenant"] != tenant:
                raise LocalError("denied")
            if target["position"] > source:
                raise LocalError("index_not_ready")
            target_position = target["position"]
        recorded = args.get("recorded_position", source)
        if recorded > source:
            raise LocalError("invalid_recorded_position")
        args = self._auto_dense(who, args)
        dense = dense_search(self.s, who, args, recorded, target_position)
        missing = [
            c
            for c in consistency.get("required_channels", ["lexical"])
            if c == "dense" and dense["availability"] != "ready"
        ]
        if missing and consistency.get("on_timeout", "error") != "partial":
            # A disabled channel cannot advance during a wait; fail without holding
            # the release gate or pretending an absent provider is merely indexed.
            raise LocalError("index_not_ready")
        scope: dict[str, Any] = {
            k: args[k]
            for k in ("project", "bindings", "task", "run", "valid_ms", "recorded_position")
            if k in args
        }
        mandatory = self.k.query(who, scope, at, constraints=True)
        if (
            args.get("freshness", "any") == "attested"
            and mandatory["status"] == "complete"
            and any(not item["attestations"] for item in mandatory["items"])
        ):
            mandatory = {"status": "blocked", "reason": "verification_unavailable", "items": []}
        policy_finished = self.s.clock.monotonic_ms()
        base = self._base(args, decision, receipt, usage)
        base.update(
            objective=args["query"],
            coverage={
                "lexical": "ready",
                "dense": dense["availability"],
                "optional": "uncalibrated",
                "required_missing": missing,
            },
            snapshot={
                "source_position": source,
                "recorded_position": recorded,
                "valid_ms": args.get("valid_ms", at),
                **({"as_of_ms": args["as_of_ms"]} if "as_of_ms" in args else {}),
            },
        )
        diag: dict[str, Any] = {
            "configuration": CONFIGURATION,
            "compiler": COMPILER,
            "selector_version": "1",
            "store": self.s.store_id,
            "incarnation": self.s.incarnation,
            "actor": who["principal"],
            "project": args["project"],
            "source_position": source,
            "source_watermark": self.s._token("watermark", tenant=tenant, position=source),
            "channel_frontiers": {
                "lexical": {"position": source, "configuration": CONFIGURATION},
                "dense": {key: value for key, value in dense.items() if key != "scores"},
            },
            "policy_epoch": tenant_row["policy_epoch"],
            "purge_epoch": tenant_row["purge_epoch"],
            "corpus_generation": source,
            "constraint_generation": mandatory.get("constraint_generation"),
            "evaluated_ms": at,
            "deadline_ms": decision.deadline_ms,
            "valid_ms": args.get("valid_ms", at),
            "recorded_position": recorded,
            **({"as_of_ms": args["as_of_ms"]} if "as_of_ms" in args else {}),
            "repository_validation": "exact_snapshot_for_declared_code_dependencies",
            "model_fingerprints": [dense["fingerprint"]] if "fingerprint" in dense else [],
            "scope_assumptions": ["local_default_workspace"],
            "cache": "not_used",
            "trace": [],
        }
        if term_window(args["query"])[1]:
            # Long queries are searched on their leading terms; the cut is disclosed.
            diag["query_terms_truncated"] = QUERY_TERM_LIMIT
        groups: list[list[dict[str, Any]]] = []
        if mandatory["status"] != "complete":
            base.update(status="blocked", reason=mandatory["reason"])
            # Only explicitly authorized conflicts are ever present in this result.
            if mandatory["reason"] == "unresolved_conflict":
                base["constraints"] = [self._claim(i) for i in mandatory["items"]]
                base["conflicts"] = mandatory.get("conflicts", [])
        else:
            base["constraints"] = [self._claim(i) for i in mandatory["items"]]
            knowledge = self.k.query(who, scope, at, constraints=False)
            from memoryking.adapters.experience import Experience

            if "recorded_position" not in args and "valid_ms" not in args:
                procedures = Experience(self.s).query(who, scope, at)
                knowledge["items"].extend(procedures["items"])
                if procedures["status"] != "complete":
                    base["warnings"].append("procedure_guidance_withheld")
                    knowledge["status"] = "blocked"
                    knowledge["reason"] = procedures.get("reason", "procedure_guidance_withheld")
            else:
                base["warnings"].append("procedures_require_current_query")
            if args.get("freshness", "any") == "attested":
                original = knowledge["items"]
                knowledge["items"] = [i for i in original if i["attestations"]]
                if len(knowledge["items"]) != len(original):
                    knowledge["status"] = "blocked"
                    knowledge["reason"] = "verification_unavailable"
            eligibility_finished = self.s.clock.monotonic_ms()
            groups, trace, cache = self._candidates(
                who,
                args,
                knowledge,
                source,
                recorded,
                at,
                dense,
                ranking,
                evaluation=ranking_override is not None,
            )
            diag.update(
                trace=[
                    {
                        "stage": "authorization_and_policy",
                        "duration_ms": max(0, policy_finished - start),
                    },
                    {
                        "stage": "claim_eligibility",
                        "duration_ms": max(0, eligibility_finished - policy_finished),
                    },
                    *trace,
                    {
                        "stage": "optional_search_expand_rank",
                        "duration_ms": max(0, self.s.clock.monotonic_ms() - eligibility_finished),
                    },
                ],
                cache=cache,
            )
            if knowledge["status"] != "complete":
                base["warnings"].append("optional_eligibility_incomplete")
            if (
                not any(t.get("visible_candidates", 0) for t in trace)
                and not any(t.get("candidate_limit_reached", False) for t in trace)
                and not base["constraints"]
                and knowledge["status"] == "complete"
                and not missing
                and "dense" not in args
            ):
                base["status"] = "no_relevant_memory"
                base["coverage"]["optional"] = "no_literal_overlap"
            else:
                # Literal overlap is useful evidence, not a held-out task-coverage calibration.
                base["status"] = "partial"
                base["warnings"].append("optional_coverage_uncalibrated")
            if knowledge.get("conflicts"):
                base["warnings"].append("optional_conflict_possible")
            if any(t.get("candidate_limit_reached", False) for t in trace):
                base["warnings"].append("optional_candidate_limit")
        raw, used, selected, omitted = pack(base, groups, count, args["max_output_tokens"])
        output = json.loads(raw)
        rendered_items = output["constraints"] + selected
        deps = self._dependencies(rendered_items)
        if ranking["state"] == "experimental":
            deps.append({"kind": "experience_record", "id": ranking["configuration"]})
        diag["ranking_experiment"] = {
            key: value for key, value in ranking.items() if key != "adjustments"
        }
        diag.update(
            status=output["status"],
            minimum_required_tokens=output.get("minimum_required_tokens"),
            budget_count_scope="failure_envelope"
            if output["status"] == "budget_insufficient"
            else "context",
            usage={**output["usage"], "output_tokens": used, "serialized_bytes": len(raw)},
            coverage=output["coverage"],
            selected=[
                {
                    "reference": i["reference"],
                    "authority": i["authority"],
                    "reason": i["selection_reason"],
                    "score_ppm": i.get("lexical_score_ppm"),
                }
                for i in rendered_items
            ],
            omissions=sorted(set(omitted)),
            duration_ms=max(0, self.s.clock.monotonic_ms() - start),
        )
        return raw, diag, deps

    def _claim(self, item: dict[str, Any]) -> dict[str, Any]:
        body = item["body"]
        return {
            "reference": item["revision"],
            "authority": item["authority"],
            "family": item["family"],
            "text": body["text"],
            "selector": body["selector"],
            "valid_start": body["valid_start"],
            "valid_end": body["valid_end"],
            "task": body["task"],
            "run": body["run"],
            "applicability": item["applicability"],
            "code_validation": item.get("code_validation", []),
            "issuer_authority": body["authority"],
            "effect": body["effect"],
            "rule_key": body["rule_key"],
            "rule_value": body["rule_value"],
            "evidence": body["evidence"],
            "assurance": "reviewed_not_code_verified",
            "attestations": item["attestations"],
            "selection_reason": "mandatory_registry"
            if body["kind"] == "constraint"
            else "literal_overlap",
            "conflicts": [],
            **(
                {
                    "procedure": body["procedure"],
                    "activation": item["activation"],
                    "procedure_family_version": item["family_version"],
                    "execution_privileges": False,
                }
                if body["kind"] == "procedure"
                else {}
            ),
        }

    def _candidates(
        self,
        who: dict[str, Any],
        args: dict[str, Any],
        knowledge: dict[str, Any],
        source: int,
        recorded: int,
        at: int,
        dense: dict[str, Any],
        ranking: dict[str, Any],
        *,
        evaluation: bool = False,
    ) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]], str]:
        query = terms(args["query"])
        anchors = anchors_in(args["query"])[:16]
        candidates = knowledge["items"]
        key = canonical_hash(
            {
                "tenant": who["tenant"],
                "principal": who["principal"],
                "store": self.s.store_id,
                "incarnation": self.s.incarnation,
                "generation": source,
                "configuration": CONFIGURATION,
                "compiler": COMPILER,
                "request": args,
                "eligible": [[i["revision"], i.get("body_hash")] for i in candidates],
            }
        )
        cache = self.s.retrieval_cache
        entry = cache.get(key)
        hit = entry is not None and at < entry["expires"]
        if hit and entry is not None:
            ids = entry["ids"]
        else:
            ids = []
            if query and args.get("freshness", "any") != "attested":
                # Tenant/scope/audience filtering precedes the candidate limit. Each term
                # gets its own newest-first window, so a common word cannot crowd out the
                # documents that match the rare ones; the windows are merged by how many
                # terms a document matches, then by recency, and the top 257 are scored.
                coverage: dict[str, int] = {}
                position: dict[str, int] = {}
                limited = False
                for term in query:
                    rows = self.db.execute(
                        "SELECT o.id,o.position FROM observations o WHERE o.tenant=? AND o.project=? AND o.deleted=0 AND o.position<=? AND (o.readers='null' OR EXISTS(SELECT 1 FROM json_each(o.readers) WHERE value=?)) AND o.id IN(SELECT id FROM lexical WHERE tenant=? AND "
                        + self.s.dialect.lexical_match()
                        + ") ORDER BY o.position DESC,o.id DESC LIMIT 257",
                        (
                            who["tenant"],
                            args["project"],
                            recorded,
                            who["principal"],
                            who["tenant"],
                            '"' + term.replace('"', '""') + '"',
                        ),
                    ).fetchall()
                    limited = limited or len(rows) > 256
                    for row in rows:
                        coverage[row["id"]] = coverage.get(row["id"], 0) + 1
                        position[row["id"]] = row["position"]
                # An anchor in the query (a path, symbol, ticket, URL, host or error code)
                # gets its own newest-first window too, and counts double in coverage:
                # naming the thing exactly outranks sharing a word with it.
                for anchor in anchors:
                    rows = self.db.execute(
                        "SELECT o.id,o.position FROM observations o WHERE o.tenant=? AND o.project=? AND o.deleted=0 AND o.position<=? AND (o.readers='null' OR EXISTS(SELECT 1 FROM json_each(o.readers) WHERE value=?)) AND o.id IN(SELECT id FROM anchors WHERE tenant=? AND anchor=?) ORDER BY o.position DESC,o.id DESC LIMIT 257",
                        (
                            who["tenant"],
                            args["project"],
                            recorded,
                            who["principal"],
                            who["tenant"],
                            anchor,
                        ),
                    ).fetchall()
                    limited = limited or len(rows) > 256
                    for row in rows:
                        coverage[row["id"]] = coverage.get(row["id"], 0) + 2
                        position[row["id"]] = row["position"]
                ordered = sorted(coverage, key=lambda i: (-coverage[i], -position[i], i))
                ids = ordered[:256] + (ordered[256:257] if limited or len(ordered) > 256 else [])
            if len(cache) >= 64:
                cache.pop(next(iter(cache)))
            cache[key] = {"expires": at + 60000, "ids": ids}
        anchor_sets: dict[str, set[str]] = {}
        if anchors and ids:
            for row in self.db.execute(
                "SELECT id,anchor FROM anchors WHERE tenant=? AND id IN ("
                + ",".join("?" for _ in ids)
                + ")",
                (who["tenant"], *ids),
            ).fetchall():
                anchor_sets.setdefault(row["id"], set()).add(row["anchor"])
        by_id = {i["revision"]: i for i in candidates}
        adjacency: dict[str, set[str]] = {i: set() for i in by_id}
        for left, right in knowledge.get("conflicts", []):
            if left in by_id and right in by_id:
                adjacency[left].add(right)
                adjacency[right].add(left)
        unresolved: set[str] = set()
        conflict_work = 0
        # Resolve registered endpoints directly, including those with no query
        # similarity. Invisible disagreement cannot be silently presented as a
        # single uncontested claim. A generic incomplete marker reveals no peer.
        for revision in by_id:
            if by_id[revision]["body"]["kind"] == "procedure":
                continue  # The procedure registry already resolved all active peers.
            edges = self.db.execute(
                "SELECT left_revision,right_revision FROM conflicts WHERE tenant=? AND position<=? AND (left_revision=? OR right_revision=?) LIMIT 257",
                (who["tenant"], recorded, revision, revision),
            ).fetchall()
            if len(edges) > 256:
                unresolved.add(revision)
                continue
            for edge in edges:
                peer = edge[1] if edge[0] == revision else edge[0]
                if peer in by_id:
                    continue  # Applicability-qualified visible edges already resolved above.
                other = self.k._revision(who["tenant"], peer)
                lifecycle, quarantine = self.k._state(who["tenant"], peer, recorded)
                if lifecycle != "accepted":
                    continue
                if other["body"] is None:
                    unresolved.add(revision)
                    continue
                body = json.loads(other["body"])
                if any(body[k] is not None and body[k] != args.get(k) for k in ("task", "run")):
                    continue
                conflict_work += len(args["bindings"]) * (
                    sum(len(c["all_of"]) for c in body["selector"]["any_of"])
                    + 128
                    * self.db.execute(
                        "SELECT count(*) FROM replacements WHERE tenant=? AND predecessor=? AND position<=?",
                        (who["tenant"], peer, recorded),
                    ).fetchone()[0]
                )
                if conflict_work > 65536:
                    unresolved.add(revision)
                    continue
                if any(
                    self.k._eligible(other, body, b, args.get("valid_ms", at), recorded, who, at)
                    != "false"
                    for b in args["bindings"]
                ):
                    unresolved.add(revision)
        # Registered same-family disagreement must be expanded independent of rank.
        # The live evaluator supplies applicability-qualified edges; suppressed or
        # unavailable endpoints never become text or counts in this trace.
        ranked: list[tuple[int, str, list[dict[str, Any]]]] = []
        visited: set[str] = set()
        for key_id, item in by_id.items():
            score = relevance(query, item["body"]["text"])
            if not score or key_id in visited:
                continue
            group_ids, pending = set(), [key_id]
            while pending:
                current = pending.pop()
                if current not in group_ids:
                    group_ids.add(current)
                    pending.extend(adjacency[current] - group_ids)
            visited |= group_ids
            group = []
            for revision in sorted(group_ids):
                claim = self._claim(by_id[revision])
                claim["lexical_score_ppm"] = relevance(query, by_id[revision]["body"]["text"])
                claim["conflicts"] = sorted(adjacency[revision])
                claim["conflict_resolution"] = (
                    "incomplete" if group_ids & unresolved else "complete"
                )
                if revision != key_id:
                    claim["selection_reason"] = "conflict_expansion"
                group.append(claim)
            score = max(relevance(query, by_id[i]["body"]["text"]) for i in group_ids)
            ranked.append((score, min(group_ids), group))
        dense_scores = dense["scores"]
        positions: dict[str, int] = {}
        for observation in dict.fromkeys([*ids[:256], *dense_scores]):
            row = self.db.execute(
                "SELECT * FROM observations WHERE tenant=? AND id=?", (who["tenant"], observation)
            ).fetchone()
            if row is None or not self.s._visible(who, row, at):
                continue
            positions[observation] = row["position"]
            raw = self._blob(row)
            # A whole atomic excerpt has attribution and bounds; never a paraphrase.
            full = raw.decode()
            score = relevance(query, full)
            anchor_hits = 0
            if anchors:
                # Exact overlap of the query's anchors with the record's, in millionths,
                # on the same scale as term overlap; the larger of the two is the score.
                anchor_hits = sum(1 for a in anchors if a in anchor_sets.get(observation, ()))
                score = max(score, anchor_hits * 1000000 // len(anchors))
            offset = 0
            if score and not relevance(query, raw[:4096].decode("utf-8", errors="ignore")):
                # Locate an actual term in the original text; avoid byte/character
                # offsets derived from casefold, which can change Unicode length.
                import re

                match = next(
                    (m for m in re.finditer(r"[^\W_]+", full) if m.group().casefold() in query),
                    None,
                )
                if match:
                    offset = len(full[: max(0, match.start() - 256)].encode())
            excerpt = raw[offset : offset + 4096].decode("utf-8", errors="ignore")
            if score or observation in dense_scores:
                ranked.append(
                    (
                        score,
                        observation,
                        [
                            {
                                "reference": observation,
                                "authority": "observed_episode",
                                "text": excerpt,
                                "actor": row["principal"],
                                "source": row["source"],
                                "observed_at_ms": row["observed_at_ms"],
                                "assurance": "actor_reported",
                                "selection_reason": "anchor_match"
                                if anchor_hits
                                else "literal_overlap"
                                if score
                                else "semantic_similarity",
                                "lexical_score_ppm": score,
                                **(
                                    {"dense_score_ppm": dense_scores[observation]}
                                    if observation in dense_scores
                                    else {}
                                ),
                                "excerpt_start_byte": offset,
                                "excerpt_end_byte": offset + len(excerpt.encode()),
                                "source_bytes": len(raw),
                                "content_hash": row["blob"],
                            }
                        ],
                    )
                )
        ranked.sort(key=lambda i: (-i[0], i[1]))
        if "dense" in args:
            # Equal-channel reciprocal-rank fusion; similarity never changes
            # authority, policy completeness or registered conflict expansion.
            lexical_order = {
                reference: rank
                for rank, (_, reference, _) in enumerate(
                    (item for item in ranked if item[0] > 0), 1
                )
            }
            dense_order = {reference: rank for rank, reference in enumerate(dense_scores, 1)}
            ranked = [
                (
                    (
                        1000000 // (60 + lexical_order[reference])
                        if reference in lexical_order
                        else 0
                    )
                    + (1000000 // (60 + dense_order[reference]) if reference in dense_order else 0),
                    reference,
                    group,
                )
                for _, reference, group in ranked
            ]
            ranked.sort(key=lambda item: (-item[0], item[1]))
        historical = "recorded_position" in args or "valid_ms" in args
        salience_trace: dict[str, Any] = {"stage": "salience_v1"}
        if historical:
            salience_trace["state"] = "disabled_for_historical_view"
        elif evaluation:
            # A ranking experiment compares two arms that must differ only by the
            # adjustment under test; use-based strength would make the first arm's
            # own delivery tilt the second.
            salience_trace["state"] = "disabled_for_ranking_evaluation"
        elif ranking["state"] != "disabled":
            # An explicit, frozen ranking policy supersedes the implicit nudge: the
            # operator's recorded decision is the only adjustment in force.
            salience_trace["state"] = "disabled_for_active_ranking_policy"
        elif args.get("salience") != "on":
            # Off unless the caller asks: a briefing's order must not depend on what
            # earlier briefings delivered unless that is what the caller wants.
            salience_trace["state"] = "off_unless_requested"
        else:
            salience = self._salience(who, args["project"], positions, at)
            archival = 0
            rescored = []
            for score, reference, group in ranked:
                item = salience.get(reference)
                if item is None:
                    rescored.append((score, reference, group))
                    continue
                for entry in group:
                    entry["salience"] = {
                        "strength_ppm": item["strength_ppm"],
                        "tier": item["tier"],
                        "deliveries_90d": item["deliveries"],
                        "helpful_votes": item["helpful"],
                    }
                score += SALIENCE_WEIGHT * item["strength_ppm"] // 1_000_000
                if item["tier"] == "archival":
                    # Still searchable and cited when nothing else matches; ranked last.
                    score -= ARCHIVAL_PENALTY
                    archival += 1
                rescored.append((score, reference, group))
            ranked = sorted(rescored, key=lambda item: (-item[0], item[1]))
            salience_trace.update(
                weight_ppm=SALIENCE_WEIGHT,
                delivery_window_days=SALIENCE_WINDOW_MS // 86_400_000,
                half_life_days=SALIENCE_HALF_LIFE_MS // 86_400_000,
                archive_after_days=ARCHIVE_AFTER_MS // 86_400_000,
                archival_candidates=archival,
            )
        if ranking["state"] == "experimental":
            from memoryking.domain.learning import adjust

            ranked = adjust(ranked, ranking["adjustments"])
        limit = args.get("top_k", 8)
        trace = [
            {"stage": "authorized_eligibility", "visible_claims": len(candidates)},
            {
                "stage": "lexical",
                "visible_candidates": len(ranked),
                "candidate_limit_reached": len(ids) > 256,
                "max_candidates": 256,
            },
            {
                "stage": "rank",
                "metric": "literal_term_overlap_v1",
                "selected_groups": min(limit, len(ranked)),
            },
        ]
        if "dense" in args:
            trace[1]["visible_candidates"] = len(lexical_order)
            trace.insert(
                2,
                {
                    "stage": "dense",
                    "visible_candidates": len(dense_scores),
                    "candidate_limit_reached": dense["candidate_limit_reached"],
                    "availability": dense["availability"],
                    "max_candidates": 256,
                },
            )
            trace[-1]["metric"] = "equal_channel_rrf60_v1"
        # The rank stage stays last among the fixed stages; salience sits before it.
        trace.insert(len(trace) - 1, salience_trace)
        if ranking["state"] != "disabled":
            trace.append(
                {
                    "stage": "bounded_ranking_experiment",
                    **{key: value for key, value in ranking.items() if key != "adjustments"},
                }
            )
        return [group for _, _, group in ranked[:limit]], trace, "hit" if hit else "miss"

    def _salience(
        self, who: dict[str, Any], project: str, positions: dict[str, int], at: int
    ) -> dict[str, dict[str, Any]]:
        """Strength from the caller's own use: how often this principal was delivered a
        record lately, whether they said it helped, and how long ago; a record nobody at
        all has used past the archive age sinks.

        Receipts and feedback are private to the principal that holds them, and another
        principal's feedback must never reorder a reader's briefing, so strength counts
        only the caller's own deliveries and votes. The archival tier looks at whether
        any principal used the record, which is a fact of the store's operation, not an
        opinion. Nothing is written or deleted; an as-of view never sees any of it."""
        import math

        if not positions:
            return {}
        wanted = set(positions)
        deliveries: dict[str, int] = {}
        last_used: dict[str, int] = {}
        used_by_anyone: set[str] = set()
        for row in self.db.execute(
            "SELECT principal,created,diagnostics,dependencies FROM rendered_artifacts WHERE tenant=? AND project=? AND state='retained' AND created>=?",
            (who["tenant"], project, at - SALIENCE_WINDOW_MS),
        ).fetchall():
            diagnostics = json.loads(row["diagnostics"] or "{}")
            if diagnostics.get("kind") == "atlas":
                continue  # looking at the graph never counts as use
            mine = row["principal"] == who["principal"]
            for dep in json.loads(row["dependencies"]):
                reference = dep.get("id")
                if reference not in wanted:
                    continue
                used_by_anyone.add(reference)
                if mine:
                    deliveries[reference] = deliveries.get(reference, 0) + 1
                    last_used[reference] = max(last_used.get(reference, 0), row["created"])
        helpful: dict[str, int] = {}
        for row in self.db.execute(
            "SELECT body FROM experience_records WHERE tenant=? AND project=? AND principal=? AND kind='feedback' AND body IS NOT NULL",
            (who["tenant"], project, who["principal"]),
        ).fetchall():
            body = json.loads(row["body"])
            reference = body.get("reference")
            if reference in wanted and body.get("usefulness") == "helpful":
                helpful[reference] = helpful.get(reference, 0) + 1
        recorded: dict[int, int] = {}
        chunk = list(positions.values())
        for start in range(0, len(chunk), 500):
            part = chunk[start : start + 500]
            for row in self.db.execute(
                "SELECT position,at_ms FROM events WHERE tenant=? AND position IN ("
                + ",".join("?" for _ in part)
                + ")",
                (who["tenant"], *part),
            ).fetchall():
                recorded[row["position"]] = row["at_ms"]
        result: dict[str, dict[str, Any]] = {}
        for reference, position in positions.items():
            count = deliveries.get(reference, 0)
            votes = helpful.get(reference, 0)
            if count:
                age = max(0, at - last_used[reference])
                recency = 0.5 ** (age / SALIENCE_HALF_LIFE_MS)
                use = min(1.0, math.log2(1 + count) / 4) * recency
            else:
                use = 0.0
            strength = min(1_000_000, int(500_000 * use) + 250_000 * min(2, votes))
            captured_ms = recorded.get(position, at)
            tier = (
                "archival"
                if reference not in used_by_anyone
                and not votes
                and at - captured_ms > ARCHIVE_AFTER_MS
                else "active"
            )
            if strength or tier == "archival":
                result[reference] = {
                    "strength_ppm": strength,
                    "tier": tier,
                    "deliveries": count,
                    "helpful": votes,
                }
        return result

    def _blob(self, row: sqlite3.Row) -> bytes:
        path = self.s.root / "blobs" / row["tenant"] / row["blob"]
        private_file(path)
        raw: bytes = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != row["blob"]:
            raise LocalError("blob_corrupt")
        return raw

    def _dependencies(self, items: list[dict[str, Any]]) -> list[dict[str, str]]:
        deps: set[tuple[str, str]] = set()
        for item in items:
            kind = "observation" if item["authority"] == "observed_episode" else "revision"
            deps.add((kind, item["reference"]))
            if item["authority"] == "active_procedure":
                deps.add(
                    (
                        "procedure_activation",
                        f"{item['reference']}|{item['activation']['position']}|{item['procedure_family_version']}",
                    )
                )
            for proof in item.get("code_validation", []):
                deps.add(("code_validation", proof["dependency"]))
                deps.update(("observation", source) for source in proof["evidence"])
            for span in item.get("evidence", []):
                deps.add(("observation", span["observation"]))
        return [{"kind": k, "id": v} for k, v in sorted(deps)]

    def _fetch(
        self, who: dict[str, Any], args: dict[str, Any], decision: Decision, receipt: str
    ) -> tuple[bytes, dict[str, Any], list[dict[str, str]]]:
        start, end = args["start_byte"], args["end_byte"]
        if end <= start or end - start > 16384:
            raise LocalError("invalid_range")
        at = decision.evaluated_ms
        if args["kind"] == "observation":
            row = self.db.execute(
                "SELECT * FROM observations WHERE tenant=? AND project=? AND id=?",
                (who["tenant"], args["project"], args["reference"]),
            ).fetchone()
            if row is None or not self.s._visible(who, row, at):
                raise LocalError("unavailable")
            raw = self._blob(row)
            authority = "observed_episode"
            deps = [{"kind": "observation", "id": row["id"]}]
        else:
            row = self.k._revision(who["tenant"], args["reference"], args["project"])
            state, quarantine = self.k._state(who["tenant"], row["id"], 9007199254740991)
            if not self.k._available(who, row, at) or quarantine:
                raise LocalError("unavailable")
            if state != "accepted":
                self.s._subject_auth(
                    self.s._token("credential", tenant=who["tenant"], principal=who["principal"]),
                    "review",
                    args["project"],
                    at,
                )
            raw = json.loads(row["body"])["text"].encode()
            authority = "revision_evidence_only"
            deps = [{"kind": "revision", "id": row["id"]}]
        if end > len(raw):
            raise LocalError("invalid_range")
        try:
            raw[:start].decode()
            excerpt = raw[start:end].decode()
        except UnicodeDecodeError as exc:
            raise LocalError("invalid_range") from exc
        count, usage = counter(args["tokenizer"])
        result = self._base(args, decision, receipt, usage)
        result.update(
            status="partial",
            reason="evidence_excerpt_not_a_briefing",
            reference=args["reference"],
            authority=authority,
            excerpt=excerpt,
            start_byte=start,
            end_byte=end,
            source_bytes=len(raw),
            source_hash=hashlib.sha256(raw).hexdigest(),
        )
        body, used = counted(result, count)
        if used > args["max_output_tokens"]:
            result.update(status="budget_insufficient", excerpt="", minimum_required_tokens=used)
            result["usage"]["budget_compliant"] = False
            body, used = counted(result, count)
            deps = []
        return (
            body,
            {
                "status": result["status"],
                "usage": {**result["usage"], "output_tokens": used, "serialized_bytes": len(body)},
                "operation": "fetch",
            },
            deps,
        )
