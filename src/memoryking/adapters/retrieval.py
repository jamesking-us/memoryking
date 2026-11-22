"""Local lexical compilation, immutable rendered artifacts and authorized inspection."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator

from memoryking.adapters.blobs import blob_path
from memoryking.adapters.corrections import Corrections
from memoryking.adapters.dense_search import claim_scores as dense_claim_scores
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
from memoryking.application import lexical_index
from memoryking.application.retention import index_receipt
from memoryking.application.store_core import StoreCore, Visibility
from memoryking.contracts.canonical import canonical_bytes, canonical_hash
from memoryking.domain.anchors import anchors_in
from memoryking.domain.briefing import (
    COMPILER,
    CONFIGURATION,
    LEGACY_CONFIGURATION,
    budget_scope,
    counted,
    pack,
    relevance,
    term_window,
    terms,
)
from memoryking.domain.briefing_text import render_brief, textualize
from memoryking.domain.duplicates import duplicates, family, shape
from memoryking.domain.lexicon import (
    QUERY_TERM_LIMIT,
    Corpus,
    Term,
    query_terms,
    rescued,
)
from memoryking.domain.lexicon import score as bm25
from memoryking.domain.local import Decision, LocalError, Released, is_user_project
from memoryking.domain.passages import EXCERPT_BYTES, split
from memoryking.domain.passages import excerpt as quote
from memoryking.domain.selectors import bindings
from memoryking.domain.timeexpr import Understanding, in_ranges, resolve

# Salience: a bounded, documented nudge from use, never a rewrite of the record.
# How the lexical and semantic channels are combined; the receipt's rank stage names it.
# Chosen on the pooled LongMemEval oracle set against equal-weight reciprocal-rank fusion
# (`equal_channel_rrf60_v1`, still selectable); see docs/reference.md. A convex combination of
# the lexical score over the best lexical score and the similarity scaled over the semantic
# candidates, equal weights; ties break as before, so the order is deterministic.
DEFAULT_FUSION: dict[str, Any] = {
    "name": "convex_lex50_v1",
    "method": "convex",
    "lexical": 1,
    "dense": 1,
}
RERANK_WINDOW = 16  # fused candidates the cross-encoder reorders
RERANK_TEXT_BYTES = 16384  # the reranker's per-text limit
SALIENCE_WEIGHT = (
    45_000  # a tenth of a typical full match (about 1/(K1+1) of the scale), in millionths
)
LEGACY_SALIENCE_WEIGHT = (
    100_000  # the same tenth, on the lexical-v1 scale where a full match is 1.0
)
SALIENCE_WINDOW_MS = 90 * 86_400_000  # deliveries older than this do not count
SALIENCE_HALF_LIFE_MS = 30 * 86_400_000  # use fades by half every thirty days
ARCHIVE_AFTER_MS = 180 * 86_400_000  # never-used records older than this rank last
ARCHIVAL_PENALTY = 4_000_000  # below any reachable score; archival stays searchable
# Time in the question: records whose time falls in a range the query names are raised by this
# share of the best score in the candidate list, so a date never beats a clearly better match
# on words but does decide between near-equal ones. A hard filter only on an explicit time_range.
TIME_BOOST_PPM = 250_000
VALIDATOR = Draft202012Validator(
    json.loads(Path(__file__).with_name("retrieval.schema.json").read_text())
)


class _Inference(Exception):
    """Control flow, never an error: the briefing needs a model's answer and must not wait for
    it inside the transaction. `execute` abandons the attempt (rolling back everything, nothing
    was persisted or released), runs `compute` with no gate and no store lock held, and starts a
    fresh attempt that finds the answer in its memo."""

    def __init__(self, key: tuple[Any, ...], compute: Callable[[], Any]):
        super().__init__("model inference outside the transaction")
        self.key, self.compute = key, compute


# Attempts that may wait for a model before it is run inside the transaction as before.
INFERENCE_ATTEMPTS = 3
# Whole-briefing retries after PostgreSQL picks this transaction as a deadlock victim.
DEADLOCK_ATTEMPTS = 3


MAX_MS = 9007199254740991


def search_text(query: str, understanding: Understanding | None) -> str:
    """The words to search: the query without its date expressions, unless nothing is left."""
    if understanding is not None and understanding.expressions:
        if terms(understanding.remaining):
            return understanding.remaining
    return query


class Retrieval:
    def __init__(self, store: StoreCore):
        self.s, self.db, self.k = store, store.db, Knowledge(store)
        # Model answers computed outside the transaction for this call; None runs a model
        # inline (callers that are not `execute`, and the last attempt).
        self.models: dict[tuple[Any, ...], Any] | None = None
        if not hasattr(store, "retrieval_cache"):
            store.retrieval_cache = {}

    def execute(self, credential: str, command: dict[str, Any]) -> Released:
        if len(canonical_bytes(command)) > 131072 or not VALIDATOR.is_valid(command):
            raise LocalError("invalid_request")
        if command["incarnation"] != self.s.incarnation:
            raise LocalError("history_changed")
        memo: dict[tuple[Any, ...], Any] = {}
        waited = deadlocks = 0
        while True:
            # The last attempt runs models inline, so the loop always ends in a release.
            self.models = memo if waited < INFERENCE_ATTEMPTS else None
            try:
                return self._execute(credential, command)
            except _Inference as need:
                waited += 1
                try:
                    memo[need.key] = need.compute()
                except LocalError as exc:
                    memo[need.key] = exc
            except Exception as exc:
                # A victim of PostgreSQL's deadlock detector wrote nothing and released nothing.
                if getattr(exc, "sqlstate", None) != "40P01" or deadlocks >= DEADLOCK_ATTEMPTS:
                    raise
                deadlocks += 1
            finally:
                self.models = None

    @contextmanager
    def _visibility(self) -> Iterator[None]:
        self.s.visibility = Visibility()
        try:
            yield
        finally:
            self.s.visibility = None

    def _execute(self, credential: str, command: dict[str, Any]) -> Released:
        action, args = command["action"], command["args"]
        access_id = uid("rcpt")
        # The memo lives inside the transaction (the store lock or the tenant gate is held),
        # so another request's briefing can never see this one's answers.
        with self.s.transaction() as decision, self._visibility():
            at = decision.evaluated_ms
            who = self.s._auth(credential, "read", args["project"], at)
            self._expire(at, who["tenant"])
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
            if action in {"brief", "fetch", "atlas", "start"}:
                if action == "start":
                    from memoryking.adapters.session_start import start

                    raw, diag, deps = start(self, who, args, decision, access_id)
                    raw, diag = self.textual(args, "brief", raw, diag)
                elif action == "atlas":
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
                "INSERT INTO receipts(tenant,id,principal,incarnation,evaluated,deadline,policy_epoch,purge_epoch,response_hash,observation_ids) VALUES (?,?,?,?,?,?,?,?,?,?)",
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
            index_receipt(self.db, who["tenant"], access_id)
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
        self.lexical_only = None
        if "dense" in args:
            return args
        if embedder is None:
            self.lexical_only = "no_model_loaded"
            return args
        row = self.db.execute(
            "SELECT 1 FROM embedding_partitions WHERE tenant=? AND project=? AND fingerprint=? AND state='experimental' AND incarnation=?",
            (who["tenant"], args["project"], embedder.fingerprint, self.s.incarnation),
        ).fetchone()
        if row is None:
            self.lexical_only = "project_not_enabled"
            return args
        key = ("embed", embedder.fingerprint, args["query"])
        if self.models is not None and key not in self.models:
            raise _Inference(key, lambda: self._embed(embedder, args["query"]))
        vector = (
            self.models[key] if self.models is not None else self._embed(embedder, args["query"])
        )
        if vector is None:  # a model failure degrades to lexical retrieval, never a refusal
            self.lexical_only = "query_embedding_failed"
            return args
        return {
            **args,
            "dense": {
                "fingerprint": embedder.fingerprint,
                "query_hash": canonical_hash(args["query"]),
                "vector": vector,
                "origin": "service_model",
            },
        }

    @staticmethod
    def _embed(embedder: Any, query: str) -> list[int] | None:
        try:
            batch = (
                embedder.embed([query], kind="query")
                if getattr(embedder, "kinds", False)
                else embedder.embed([query])
            )
            return cast("list[int]", batch.vectors[0])
        except Exception:
            return None

    @staticmethod
    def renderer(args: dict[str, Any]) -> Callable[[dict[str, Any]], str] | None:
        """What the budget counts: the text the model reads when the caller asked for text."""
        if args.get("format") != "text":
            return None
        return lambda packet: render_brief(packet)[0]

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

    def _expire(self, at: int, tenant: str) -> None:
        # The tenant predicate lets the partial index over retained rows answer it.
        self.db.execute(
            "UPDATE rendered_artifacts SET state='expired',body=NULL,request_json=NULL,diagnostics=NULL,dependencies='[]' WHERE tenant=? AND state='retained' AND expires<=?",
            (tenant, at),
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
            "INSERT INTO rendered_artifacts(tenant,id,principal,project,incarnation,created,expires,state,body,body_hash,request_json,diagnostics,dependencies) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
        lexical_current = lexical_index.state(self.db, tenant)[0] == lexical_index.CURRENT
        configuration = CONFIGURATION if lexical_current else LEGACY_CONFIGURATION
        if "as_of_ms" in args:
            # One instant stands for both clocks: the history frontier as it was then
            # (the last event recorded by that wall time) and the claim validity time.
            if "recorded_position" in args or "valid_ms" in args:
                raise LocalError("invalid_request")
            # Event times never decrease with position (the clock refuses to go back), so
            # the last event at or before the instant is the newest one the index finds.
            latest = self.db.execute(
                "SELECT position FROM events WHERE tenant=? AND at_ms<=? ORDER BY at_ms DESC,position DESC LIMIT 1",
                (tenant, args["as_of_ms"]),
            ).fetchone()
            frontier = 0 if latest is None else latest[0]
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
        # Dates in the question, resolved against the briefing's reference instant.
        understanding = resolve(args["query"], args.get("as_of_ms", at))
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
            "configuration": configuration,
            "compiler": COMPILER,
            "selector_version": "1",
            "store": self.s.store_id,
            "incarnation": self.s.incarnation,
            "actor": who["principal"],
            "project": args["project"],
            "source_position": source,
            "source_watermark": self.s._token("watermark", tenant=tenant, position=source),
            "channel_frontiers": {
                "lexical": {"position": source, "configuration": configuration},
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
        if term_window(search_text(args["query"], understanding))[1]:
            # Long queries are searched on their leading terms; the cut is disclosed.
            diag["query_terms_truncated"] = QUERY_TERM_LIMIT
        groups: list[list[dict[str, Any]]] = []
        behind: dict[str, list[str]] = {}
        drafted: list[list[dict[str, Any]]] = []
        if mandatory["status"] != "complete":
            base.update(status="blocked", reason=mandatory["reason"])
            if mandatory.get("unbound_fields"):
                base["unbound_fields"] = mandatory["unbound_fields"]
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
            groups, trace, cache, behind = self._candidates(
                who,
                args,
                knowledge,
                source,
                recorded,
                at,
                dense,
                ranking,
                evaluation=ranking_override is not None,
                lexical_current=lexical_current,
                configuration=configuration,
                understanding=understanding,
            )
            if (
                "recorded_position" not in args
                and "valid_ms" not in args
                and args.get("freshness", "any") != "attested"
            ):
                # Off unless an admin turned the project's tier on; then unreviewed drafts
                # follow the accepted guidance in a section of their own.
                from memoryking.adapters.unreviewed import Unreviewed

                drafted = Unreviewed(self.s).groups(who, args, at, recorded)
                if drafted:
                    groups = [*groups, *drafted]
                    base["warnings"].append("unreviewed_drafts_included")
            for stage in trace:
                if stage.get("stage") == "rerank_v1" and "model" in stage:
                    diag["model_fingerprints"].append(stage["model"])
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
                and not drafted
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
        raw, used, selected, omitted = pack(
            base, groups, count, args["max_output_tokens"], self.renderer(args)
        )
        output = json.loads(raw)
        rendered_items = output["constraints"] + selected
        deps = self._dependencies(rendered_items)
        for item in rendered_items:
            # Records an item stands for (duplicates, older versions, cited evidence) are
            # still what the briefing depends on: a purge of any of them retires it.
            for reference in behind.get(item["reference"], []):
                if {"kind": "observation", "id": reference} not in deps:
                    deps.append({"kind": "observation", "id": reference})
        deps.sort(key=lambda d: (d["kind"], d["id"]))
        if ranking["state"] == "experimental":
            deps.append({"kind": "experience_record", "id": ranking["configuration"]})
        diag["ranking_experiment"] = {
            key: value for key, value in ranking.items() if key != "adjustments"
        }
        diag.update(
            status=output["status"],
            minimum_required_tokens=output.get("minimum_required_tokens"),
            budget_count_scope=budget_scope(output),
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
            **({"scope": "user"} if is_user_project(item.get("project", "")) else {}),
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

    SCOPE = (
        "SELECT o.id,o.position FROM observations o WHERE o.tenant=? AND o.project=? AND o.deleted=0 "
        "AND o.position<=? AND (o.readers='null' OR EXISTS(SELECT 1 FROM json_each(o.readers) WHERE value=?)) "
        "AND NOT EXISTS(SELECT 1 FROM observation_holds h WHERE h.tenant=o.tenant AND h.id=o.id AND h.state='held') AND "
    )

    @staticmethod
    def tier_sizes(count: int) -> list[int]:
        """How many of the rarest terms each conjunction keeps: all of them, then fewer.

        The first few steps drop one term at a time (the most common goes first); later
        steps halve, so a long query costs a bounded number of searches."""
        if count <= 0:
            return []
        sizes = list(range(count, max(count - 4, 0), -1))
        size = count - 4
        while size > 1:
            size = (size + 1) // 2
            sizes.append(size)
        if sizes[-1] != 1:
            sizes.append(1)
        return list(dict.fromkeys(sizes))

    @staticmethod
    def _when(args: dict[str, Any]) -> tuple[str, tuple[int, ...]]:
        """The SQL clause (and its parameters) for an explicit `time_range`, else nothing."""
        window = args.get("time_range")
        if not window:
            return "", ()
        return "o.observed_at_ms>=? AND o.observed_at_ms<=? AND ", (
            window.get("from_ms", 0),
            window.get("to_ms", MAX_MS),
        )

    @staticmethod
    def _time_boost(
        understanding: Understanding | None,
        window: dict[str, int] | None,
        ranked: list[tuple[int, str, list[dict[str, Any]]]],
        observed: dict[str, int],
        rank_key: Any,
    ) -> tuple[dict[str, Any], list[tuple[int, str, list[dict[str, Any]]]]]:
        """Raise matches whose time is in a range the question names (never filters).

        An observation counts by its observed time; a claim by its validity interval, and only
        when that interval has a bound (an open-ended claim is in no particular time)."""
        trace: dict[str, Any] = {"stage": "time_v1", "language": "en"}
        if window:
            trace["explicit_range"] = {
                "from_ms": window.get("from_ms", 0),
                "to_ms": window.get("to_ms", MAX_MS),
                "filter": "hard",
            }
        if understanding is None or not understanding.expressions:
            trace["state"] = "no_time_in_query"
            return trace, ranked
        trace.update(understanding.trace())
        ranges = understanding.ranges
        top = max((score for score, _, _ in ranked), default=0)
        bonus = top * TIME_BOOST_PPM // 1_000_000
        boosted = 0
        rescored = []
        for score, reference, group in ranked:
            hit = False
            for entry in group:
                if entry.get("authority") == "observed_episode":
                    when = observed.get(reference)
                    hit = hit or (when is not None and in_ranges(ranges, when, when))
                elif entry.get("valid_start") is not None or entry.get("valid_end") is not None:
                    hit = hit or in_ranges(ranges, entry["valid_start"], entry["valid_end"])
            if hit and score > 0:
                for entry in group:
                    entry["in_time_range"] = True
                score += bonus
                boosted += 1
            rescored.append((score, reference, group))
        trace.update(state="applied", boost_ppm_of_best=TIME_BOOST_PPM, boosted=boosted)
        return trace, sorted(rescored, key=rank_key)

    def _select(
        self,
        who: dict[str, Any],
        args: dict[str, Any],
        recorded: int,
        qterms: list[Term],
        corpus: Corpus,
        anchors: list[str],
    ) -> tuple[list[str], dict[str, Any]]:
        """Candidate observations by informativeness, not recency.

        Exact anchors first. Then the documents holding every query term, then holding all
        but the most common one, and so on down to the rarest term alone, then the other
        terms one at a time from rare to common; within a step the newest come first.
        Every search carries the full authorization filter (tenant, project, not deleted,
        recorded position, readers, not held) before its limit, so a reader never competes
        with, or is crowded out by, records they cannot read."""
        tenant, project = who["tenant"], args["project"]
        when, bounds = self._when(args)
        scope = (tenant, project, recorded, who["principal"], *bounds)
        chosen: dict[str, int] = {}
        if anchors:
            hits: dict[str, int] = {}
            position: dict[str, int] = {}
            for anchor in anchors:
                for row in self.db.execute(
                    self.SCOPE
                    + when
                    + "o.id IN(SELECT id FROM anchors WHERE tenant=? AND anchor=?) ORDER BY o.position DESC,o.id DESC LIMIT 257",
                    (*scope, tenant, anchor),
                ).fetchall():
                    hits[row["id"]] = hits.get(row["id"], 0) + 1
                    position[row["id"]] = row["position"]
            for found in sorted(hits, key=lambda i: (-hits[i], -position[i], i))[:128]:
                chosen[found] = position[found]
        # Rarest first; a term no document holds (frequency zero) goes last, so it is the
        # first one dropped and never empties a search that other terms could fill.
        ordered = sorted(
            qterms, key=lambda t: (corpus.frequency(t.key) == 0, corpus.frequency(t.key), t.key)
        )
        searches = [ordered[:n] for n in self.tier_sizes(len(ordered))]
        searches += [[term] for term in ordered[1:7]]
        ran = 0
        for group in searches:
            if len(chosen) > 256:
                break
            expression = self.s.dialect.lexical_expression([t.alternatives() for t in group])
            ran += 1
            for row in self.db.execute(
                self.SCOPE
                + when
                + "o.id IN(SELECT id FROM lexical WHERE tenant=? AND "
                + self.s.dialect.lexical_boolean()
                + ") ORDER BY o.position DESC,o.id DESC LIMIT ?",
                (*scope, tenant, expression, 257 + len(chosen)),
            ).fetchall():
                chosen.setdefault(row["id"], row["position"])
        ids = list(chosen)
        ids = ids[:256] + (ids[256:257] if len(ids) > 256 else [])
        return ids, {"searches": ran, "terms": len(qterms)}

    def _legacy_select(
        self,
        who: dict[str, Any],
        args: dict[str, Any],
        recorded: int,
        query: list[str],
        anchors: list[str],
    ) -> list[str]:
        """The pre-lexical-v2 selection, kept for a store whose index is not rebuilt yet."""
        tenant = who["tenant"]
        when, bounds = self._when(args)
        scope = (tenant, args["project"], recorded, who["principal"], *bounds)
        coverage: dict[str, int] = {}
        position: dict[str, int] = {}
        limited = False
        for term in query:
            rows = self.db.execute(
                self.SCOPE
                + when
                + "o.id IN(SELECT id FROM lexical WHERE tenant=? AND "
                + self.s.dialect.lexical_match()
                + ") ORDER BY o.position DESC,o.id DESC LIMIT 257",
                (*scope, tenant, '"' + term.replace('"', '""') + '"'),
            ).fetchall()
            limited = limited or len(rows) > 256
            for row in rows:
                coverage[row["id"]] = coverage.get(row["id"], 0) + 1
                position[row["id"]] = row["position"]
        # An anchor in the query gets its own newest-first window and counts double.
        for anchor in anchors:
            rows = self.db.execute(
                self.SCOPE
                + when
                + "o.id IN(SELECT id FROM anchors WHERE tenant=? AND anchor=?) ORDER BY o.position DESC,o.id DESC LIMIT 257",
                (*scope, tenant, anchor),
            ).fetchall()
            limited = limited or len(rows) > 256
            for row in rows:
                coverage[row["id"]] = coverage.get(row["id"], 0) + 2
                position[row["id"]] = row["position"]
        ordered = sorted(coverage, key=lambda i: (-coverage[i], -position[i], i))
        return ordered[:256] + (ordered[256:257] if limited or len(ordered) > 256 else [])

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
        lexical_current: bool = False,
        configuration: str = LEGACY_CONFIGURATION,
        understanding: Understanding | None = None,
    ) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]], str, dict[str, list[str]]]:
        tenant = who["tenant"]
        # Date words are not search words: "last week" must not look for "week".
        words = search_text(args["query"], understanding)
        query = terms(words)
        qterms = query_terms(words) if lexical_current else []
        window = args.get("time_range")
        if window:
            span = [(window.get("from_ms", 0), window.get("to_ms", MAX_MS))]
            knowledge = {
                **knowledge,
                "items": [
                    i
                    for i in knowledge["items"]
                    if in_ranges(span, i["body"]["valid_start"], i["body"]["valid_end"])
                ],
            }
        anchors = anchors_in(args["query"])[:16]
        candidates = knowledge["items"]
        # Term weights come from whole-project statistics (not what this reader may see)
        # and only ever shape order; they are never copied into a packet or trace. A
        # historical view uses today's statistics, which the trace says.
        corpus = (
            lexical_index.corpus(self.db, tenant, args["project"], [t.key for t in qterms])
            if qterms
            else Corpus()
        )

        def scorer(text: str) -> int:
            return bm25(qterms, text, corpus) if lexical_current else relevance(query, text)

        # The principal's own scope, when it acts for a user: matching observations from it
        # join the project's, labelled, and the cached selection depends on which scope it is.
        user = self.s.user_scope(who, "read", at)
        key = canonical_hash(
            {
                "tenant": who["tenant"],
                "principal": who["principal"],
                "store": self.s.store_id,
                "incarnation": self.s.incarnation,
                "generation": source,
                "configuration": configuration,
                "compiler": COMPILER,
                "request": args,
                "user_scope": user,
                "eligible": [[i["revision"], i.get("body_hash")] for i in candidates],
            }
        )
        cache = self.s.retrieval_cache
        entry = cache.get(key)
        hit = entry is not None and at < entry["expires"]
        selection: dict[str, Any] = {}
        if hit and entry is not None:
            ids = entry["ids"]
            selection = entry.get("selection", {})
        else:
            ids = []
            if query and args.get("freshness", "any") != "attested":
                if lexical_current:
                    ids, selection = self._select(who, args, recorded, qterms, corpus, anchors)
                else:
                    ids = self._legacy_select(who, args, recorded, query, anchors)
                if user is not None and user != args["project"]:
                    mine = {**args, "project": user}
                    extra = (
                        self._select(who, mine, recorded, qterms, corpus, anchors)[0]
                        if lexical_current
                        else self._legacy_select(who, mine, recorded, query, anchors)
                    )
                    ids = list(dict.fromkeys([*ids, *extra]))
            if len(cache) >= 64:
                cache.pop(next(iter(cache)))
            cache[key] = {"expires": at + 60000, "ids": ids, "selection": selection}
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
        # Equal scores order by authority (accepted knowledge before unreviewed
        # observations), then newest recorded first, then the reference for determinism.
        tie: dict[str, tuple[int, int, str]] = {}
        visited: set[str] = set()
        claim_dense: dict[str, int] = {}
        if "dense" in args and by_id:
            claim_dense = dense_claim_scores(
                self.s, who, args, list(by_id), len(args["dense"]["vector"])
            )
        for key_id, item in by_id.items():
            score = scorer(item["body"]["text"])
            if (not score and key_id not in claim_dense) or key_id in visited:
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
                claim["lexical_score_ppm"] = scorer(by_id[revision]["body"]["text"])
                claim["conflicts"] = sorted(adjacency[revision])
                claim["conflict_resolution"] = (
                    "incomplete" if group_ids & unresolved else "complete"
                )
                if revision in claim_dense:
                    claim["dense_score_ppm"] = claim_dense[revision]
                if revision != key_id:
                    claim["selection_reason"] = "conflict_expansion"
                elif not claim["lexical_score_ppm"]:
                    claim["selection_reason"] = "semantic_similarity"
                group.append(claim)
            score = max(scorer(by_id[i]["body"]["text"]) for i in group_ids)
            lead = min(group_ids)
            tie[lead] = (0, -max(by_id[i].get("created_position", 0) for i in group_ids), lead)
            ranked.append((score, lead, group))
        dense_scores = dense["scores"]
        positions: dict[str, int] = {}
        families: dict[str, tuple[str, str, str]] = {}
        observed: dict[str, int] = {}
        # Corrections: a record with a visible replacement, or one this reader asked to have
        # retired, is left out; one someone else asked to retire is marked (see corrections.py).
        corrected, retiring = Corrections(self.s).screen(
            who, list(dict.fromkeys([*ids[:256], *dense_scores])), recorded, at
        )
        considered = [o for o in dict.fromkeys([*ids[:256], *dense_scores]) if o not in corrected]
        rows = self.s.observation_rows(who["tenant"], considered)
        for observation in considered:
            row = rows.get(observation)
            if row is None or not self.s._visible(who, row, at):
                continue
            if window and not (
                window.get("from_ms", 0) <= row["observed_at_ms"] <= window.get("to_ms", MAX_MS)
            ):
                continue
            observed[observation] = row["observed_at_ms"]
            positions[observation] = row["position"]
            families[observation] = family(row["principal"], row["source"], row["source_event"])
            tie[observation] = (1, -row["position"], observation)
            raw = self._blob(row)
            # A whole atomic excerpt has attribution and bounds; never a paraphrase.
            full = raw.decode()
            score = scorer(full)
            # Only a long observation is read as passages: to quote the one that answers,
            # and (lexical-v2 rescue) to score a long capture by its best passage when that
            # beats the whole, which length normalisation would otherwise bury.
            parts = split(full) if len(raw) > EXCERPT_BYTES else []
            passage_scores = [0] * len(parts)
            if parts and (score or lexical_current):
                passage_scores = [scorer(full[p.start : p.end]) for p in parts]
                if lexical_current:
                    score = rescued(score, passage_scores)
            anchor_hits = 0
            if anchors:
                # Exact overlap of the query's anchors with the record's, in millionths,
                # on the same scale as term overlap; the larger of the two is the score.
                anchor_hits = sum(1 for a in anchors if a in anchor_sets.get(observation, ()))
                score = max(score, anchor_hits * 1000000 // len(anchors))
                if anchor_hits and parts:
                    # An anchor hit steers the quote to the passage that names it.
                    folded = [full[p.start : p.end].casefold() for p in parts]
                    passage_scores = [
                        max(
                            value,
                            sum(1 for a in anchors if a.casefold() in text)
                            * 1000000
                            // len(anchors),
                        )
                        for value, text in zip(passage_scores, folded, strict=True)
                    ]
            first, last = quote(full, parts, passage_scores) if parts else (0, len(full))
            excerpt_text = full[first:last]
            offset = len(full[:first].encode())
            if score or observation in dense_scores:
                ranked.append(
                    (
                        score,
                        observation,
                        [
                            {
                                "reference": observation,
                                "authority": "observed_episode",
                                "text": excerpt_text,
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
                                    {"retirement_requested": True}
                                    if observation in retiring
                                    else {}
                                ),
                                **(
                                    {"dense_score_ppm": dense_scores[observation]}
                                    if observation in dense_scores
                                    else {}
                                ),
                                "excerpt_start_byte": offset,
                                "excerpt_end_byte": offset + len(excerpt_text.encode()),
                                "source_bytes": len(raw),
                                "content_hash": row["blob"],
                                **({"scope": "user"} if is_user_project(row["project"]) else {}),
                            }
                        ],
                    )
                )
        rank_key = lambda item: (-item[0], tie.get(item[1], (2, 0, item[1])))  # noqa: E731
        ranked.sort(key=rank_key)
        fusion = getattr(self.s, "fusion", None) or DEFAULT_FUSION
        if "dense" in args:
            # Similarity never changes authority, policy completeness or registered
            # conflict expansion; it only orders what is already eligible.
            dense_all = dict(dense_scores)
            for _, reference, group in ranked:
                best = max((c.get("dense_score_ppm", 0) for c in group), default=0)
                if reference not in dense_all and best:
                    dense_all[reference] = best
            dense_order = {
                reference: rank
                for rank, reference in enumerate(
                    sorted(dense_all, key=lambda r: (-dense_all[r], r)), 1
                )
            }
            lexical_order = {
                reference: rank
                for rank, (_, reference, _) in enumerate(
                    (item for item in ranked if item[0] > 0), 1
                )
            }
            top_lexical = max((item[0] for item in ranked), default=0) or 1
            d_low, d_high = min(dense_all.values(), default=0), max(dense_all.values(), default=0)
            spread = (d_high - d_low) or 1
            lexical_weight, dense_weight = fusion["lexical"], fusion["dense"]

            def fused(score: int, reference: str) -> int:
                return int(fuse(score, reference))

            def fuse(score: int, reference: str) -> Any:
                if fusion["method"] == "convex":
                    lex = score / top_lexical if reference in lexical_order else 0.0
                    sem = (
                        (dense_all[reference] - d_low) / spread if reference in dense_order else 0.0
                    )
                    return round(
                        1_000_000
                        * (lexical_weight * lex + dense_weight * sem)
                        / (lexical_weight + dense_weight)
                    )
                k = fusion["k"]
                return round(
                    (lexical_weight * 1_000_000 // (k + lexical_order[reference]))
                    if reference in lexical_order
                    else 0
                ) + round(
                    (dense_weight * 1_000_000 // (k + dense_order[reference]))
                    if reference in dense_order
                    else 0
                )

            ranked = [
                (fused(score, reference), reference, group) for score, reference, group in ranked
            ]
            ranked.sort(key=rank_key)
        time_trace, ranked = self._time_boost(understanding, window, ranked, observed, rank_key)
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
            salience_weight = SALIENCE_WEIGHT if lexical_current else LEGACY_SALIENCE_WEIGHT
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
                score += salience_weight * item["strength_ppm"] // 1_000_000
                if item["tier"] == "archival":
                    # Still searchable and cited when nothing else matches; ranked last.
                    score -= ARCHIVAL_PENALTY
                    archival += 1
                rescored.append((score, reference, group))
            ranked = sorted(rescored, key=rank_key)
            salience_trace.update(
                weight_ppm=salience_weight,
                delivery_window_days=SALIENCE_WINDOW_MS // 86_400_000,
                half_life_days=SALIENCE_HALF_LIFE_MS // 86_400_000,
                archive_after_days=ARCHIVE_AFTER_MS // 86_400_000,
                archival_candidates=archival,
            )
        rerank_trace: dict[str, Any] = {"stage": "rerank_v1"}
        reranker = self.s.reranker
        if args.get("rerank") != "on":
            # Off unless asked: a model's opinion of relevance enters a briefing only
            # when the caller wants it, and the trace says so either way.
            rerank_trace["state"] = "off_unless_requested"
        elif evaluation:
            rerank_trace["state"] = "disabled_for_ranking_evaluation"
        elif ranking["state"] not in {"disabled", "historical_override_disabled"}:
            rerank_trace["state"] = "disabled_for_active_ranking_policy"
        elif reranker is None:
            rerank_trace["state"] = "unavailable_no_model"
        else:
            # The cross-encoder reads the query against each of the top fused candidates
            # and reorders that window only; everything below keeps the fused order, and
            # a model failure keeps it too. Authority, policy and conflict expansion are
            # untouched: the model moves records, it never admits or removes one.
            started = self.s.clock.monotonic_ms()
            window = getattr(self.s, "rerank_window", None) or RERANK_WINDOW
            head = ranked[:window]
            texts = [
                (group[0].get("text") or reference)
                .encode()[:RERANK_TEXT_BYTES]
                .decode("utf-8", errors="ignore")
                or reference
                for _, reference, group in head
            ]
            scoring = ("rerank", reranker.fingerprint, args["query"], tuple(texts))
            if head and self.models is not None and scoring not in self.models:
                # Scored with no gate or lock held; the next attempt decides afresh and finds
                # the scores here only if it ranks the same window (a pure function of text).
                raise _Inference(scoring, lambda: reranker.score(args["query"], texts))
            try:
                if not head:
                    scores = []
                elif self.models is not None:
                    scores = self.models[scoring]
                    if isinstance(scores, LocalError):
                        raise scores
                else:
                    scores = reranker.score(args["query"], texts)
            except LocalError as exc:
                rerank_trace.update(state="failed_fused_order_kept", error=exc.code)
            else:
                order = sorted(range(len(head)), key=lambda i: (-scores[i], i))
                for index, (_, _, group) in enumerate(head):
                    for entry in group:
                        entry["rerank_score_ppm"] = scores[index]
                ranked = [head[i] for i in order] + ranked[len(head) :]
                rerank_trace.update(
                    state="applied",
                    model=reranker.fingerprint,
                    window=window,
                    candidates=len(head),
                    moved=sum(1 for new, old in enumerate(order) if new != old),
                    duration_ms=max(0, self.s.clock.monotonic_ms() - started),
                )
        if ranking["state"] == "experimental":
            from memoryking.domain.learning import adjust

            ranked = adjust(ranked, ranking["adjustments"])
        limit = args.get("top_k", 8)
        presented, collapsed, behind = self._present(who, ranked, limit, positions, families)
        trace = [
            {"stage": "authorized_eligibility", "visible_claims": len(candidates)},
            {
                "stage": "lexical",
                "visible_candidates": len(ranked),
                "candidate_limit_reached": len(ids) > 256,
                "max_candidates": 256,
                "configuration": configuration,
                **(
                    {
                        "selection": "informative_tiers_v1",
                        "searches": selection.get("searches", 0),
                        "statistics": "whole_project_current_corpus",
                    }
                    if lexical_current
                    else {
                        "selection": "per_term_recency_windows_v1",
                        "statistics": "unavailable_until_reindex",
                    }
                ),
            },
            {
                "stage": "rank",
                "metric": "bm25_word_forms_v1" if lexical_current else "literal_term_overlap_v1",
                "selected_groups": len(presented),
            },
        ]
        if "dense" not in args and getattr(self, "lexical_only", None):
            # No semantic channel this time, and the trace says why: briefings use it
            # automatically whenever a model is loaded and the project is enabled.
            trace.insert(
                2,
                {
                    "stage": "dense",
                    "availability": "unavailable",
                    "reason": self.lexical_only,
                },
            )
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
                    **{
                        key: dense[key]
                        for key in ("scan", "eligible_vectors", "scan_truncated", "scan_limit")
                        if key in dense
                    },
                },
            )
            trace[-1]["metric"] = fusion["name"]
            trace[-1]["fusion"] = {k: v for k, v in fusion.items() if k != "name"}
        # The rank stage stays last among the fixed stages; salience and the reranker
        # sit before it, in the order they ran.
        trace.insert(len(trace) - 1, time_trace)
        trace.insert(len(trace) - 1, salience_trace)
        trace.insert(len(trace) - 1, rerank_trace)
        trace.insert(len(trace) - 1, {"stage": "one_answer_per_fact", **collapsed})
        if ranking["state"] != "disabled":
            trace.append(
                {
                    "stage": "bounded_ranking_experiment",
                    **{key: value for key, value in ranking.items() if key != "adjustments"},
                }
            )
        return presented, trace, "hit" if hit else "miss", behind

    REFERENCES_SHOWN = 16  # references named in an item's own list; the receipt depends on all

    def _present(
        self,
        who: dict[str, Any],
        ranked: list[tuple[int, str, list[dict[str, Any]]]],
        limit: int,
        positions: dict[str, int],
        families: dict[str, tuple[str, str, str]],
    ) -> tuple[list[list[dict[str, Any]]], dict[str, int], dict[str, list[str]]]:
        """One answer per fact, after ranking and before packing.

        Only the item list changes, never what is authorized: every record that was
        collapsed stays a dependency of the receipt (the kept item names it), so purging
        or hiding any of them retires the briefing exactly as before.

        1. Several versions of one memory-tool file or vault note (`name`, `name__v2`, ...):
           the newest is kept and names the older ones in `supersedes`.
        2. Duplicates (see `domain.duplicates`): the newest record's text is kept, at the
           best rank any of them had, and `also_recorded_in` names the others.
        3. An accepted claim in the briefing is the answer for the observations it cites
           when they hold nothing it lacks: the excerpt lies inside the cited span, or
           nearly all its words are in the claim. They are listed in the claim's
           `evidence_not_repeated`; the claim still cites them as its evidence."""
        count = {"duplicates_collapsed": 0, "versions_collapsed": 0, "evidence_not_repeated": 0}
        Entry = tuple[int, str, list[dict[str, Any]]]

        def single(entry: Entry) -> bool:
            return len(entry[2]) == 1 and entry[2][0]["authority"] == "observed_episode"

        every: dict[int, list[str]] = {}  # all references behind an item, for the receipt

        def link(item: dict[str, Any], field: str, references: list[str]) -> None:
            if references:
                found = item.get(field, {"count": 0, "references": []})
                item[field] = {
                    "count": found["count"] + len(references),
                    "references": [*found["references"], *references][: self.REFERENCES_SHOWN],
                }
                every.setdefault(id(item), []).extend(references)

        def newest(entries: list[Entry], members: list[int]) -> int:
            return max(members, key=lambda i: (positions[entries[i][1]], entries[i][1]))

        def fold(entries: list[Entry], groups: list[list[int]]) -> list[Entry]:
            """Each group of indexes becomes its newest member, at the group's first slot."""
            keep = {min(g): newest(entries, g) for g in groups if len(g) > 1}
            gone = {i for g in groups if len(g) > 1 for i in g}
            result: list[Entry] = []
            for index, entry in enumerate(entries):
                if index in keep:
                    result.append((entry[0], entries[keep[index]][1], entries[keep[index]][2]))
                elif index not in gone:
                    result.append(entry)
            return result

        # Only the head of the ranking can reach the briefing once repeats are folded, so
        # only it is compared (a comparison is quadratic); the rest keeps its order.
        entries: list[Entry] = list(ranked[: min(3 * limit + 8, 64)])
        rest: list[Entry] = list(ranked[min(3 * limit + 8, 64) :])
        # 1. versions of one file
        by_family: dict[tuple[str, str, str], list[int]] = {}
        for index, entry in enumerate(entries):
            if single(entry) and entry[1] in families:
                by_family.setdefault(families[entry[1]], []).append(index)
        for members in by_family.values():
            if len(members) > 1:
                keep = newest(entries, members)
                older = [i for i in members if i != keep]
                link(entries[keep][2][0], "supersedes", [entries[i][1] for i in older])
                count["versions_collapsed"] += len(older)
        entries = fold(entries, list(by_family.values()))
        # 2. duplicates
        shapes: dict[str, Any] = {}
        clusters: list[list[int]] = []
        for index, entry in enumerate(entries):
            if not single(entry):
                continue
            shapes[entry[1]] = shape(entry[2][0]["text"])
            for cluster in clusters:
                # a question that names a date keeps a copy in that range apart from the rest
                same_time = entries[cluster[0]][2][0].get("in_time_range") == entry[2][0].get(
                    "in_time_range"
                )
                if same_time and duplicates(shapes[entries[cluster[0]][1]], shapes[entry[1]]):
                    cluster.append(index)
                    break
            else:
                clusters.append([index])
        for cluster in clusters:
            if len(cluster) > 1:
                keep = newest(entries, cluster)
                others = sorted(
                    (i for i in cluster if i != keep),
                    key=lambda i: (-positions[entries[i][1]], entries[i][1]),
                )
                item = entries[keep][2][0]
                link(item, "also_recorded_in", [entries[i][1] for i in others])
                for i in others:  # versions an older copy had superseded stay named
                    prior = entries[i][2][0]
                    if "supersedes" in prior:
                        link(item, "supersedes", every[id(prior)])
                count["duplicates_collapsed"] += len(others)
        entries = fold(entries, clusters) + rest
        # 3. observations an accepted claim already answers
        while True:
            covering: dict[str, list[tuple[dict[str, Any], list[tuple[int, int]]]]] = {}
            for _, _, group in entries[:limit]:
                for item in group:
                    if item["authority"] == "observed_episode":
                        continue
                    for span in item.get("evidence") or []:
                        covering.setdefault(span["observation"], []).append(
                            (item, [(span["start_byte"], span["end_byte"])])
                        )
            redundant: dict[str, dict[str, Any]] = {}
            for entry in entries[:limit]:
                if not single(entry) or entry[1] not in covering:
                    continue
                item = entry[2][0]
                words = shapes[entry[1]].words if entry[1] in shapes else shape(item["text"]).words
                for claim, spans in covering[entry[1]]:
                    inside = any(
                        a <= item["excerpt_start_byte"] and item["excerpt_end_byte"] <= b
                        for a, b in spans
                    )
                    known = shape(claim["text"]).words
                    if inside or (words and len(words & known) / len(words) >= 0.9):
                        redundant[entry[1]] = claim
                        break
            if not redundant:
                break
            for reference, claim in redundant.items():
                link(claim, "evidence_not_repeated", [reference])
            entries = [e for e in entries if e[1] not in redundant]
            count["evidence_not_repeated"] += len(redundant)
        chosen = entries[:limit]
        shown = [positions[e[1]] for e in chosen if single(e) and e[1] in positions]
        recorded: dict[int, int] = {}
        if shown:
            for row in self.db.execute(
                "SELECT position,at_ms FROM events WHERE tenant=? AND position IN ("
                + ",".join("?" for _ in shown)
                + ")",
                (who["tenant"], *shown),
            ).fetchall():
                recorded[row[0]] = row[1]
        for entry in chosen:
            if single(entry) and positions.get(entry[1]) in recorded:
                entry[2][0]["recorded_ms"] = recorded[positions[entry[1]]]
        collapsed = {
            entry[2][0]["reference"]: every[id(entry[2][0])]
            for entry in chosen
            if id(entry[2][0]) in every
        }
        return [group for _, _, group in chosen], count, collapsed

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
        path = blob_path(self.s.root, row["tenant"], row["blob"])
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
