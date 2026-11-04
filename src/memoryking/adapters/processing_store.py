"""Optional source-backed proposals and conservative context compilation.

Generation is outside the release transaction and is never invoked here. Supplied
model provenance is an actor attestation, not evidence of dispatch or entitlement.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from memoryking.adapters.knowledge_store import Knowledge
from memoryking.adapters.local_store import RETRY_MS, USAGE_LIMIT, uid
from memoryking.adapters.token_count import counter
from memoryking.adapters.working import Working
from memoryking.application.store_core import StoreCore
from memoryking.contracts.canonical import canonical_bytes, canonical_hash
from memoryking.domain.briefing import counted
from memoryking.domain.compression import validate
from memoryking.domain.local import Decision, LocalError, Released
from memoryking.domain.usage import UsageRecord, summarize

VALIDATOR = Draft202012Validator(
    json.loads(Path(__file__).with_name("processing.schema.json").read_text())
)


class Processing:
    def __init__(self, store: StoreCore):
        self.s, self.db = store, store.db
        self.k, self.w = Knowledge(store), Working(store)
        self.r = self.w.r

    def execute(self, credential: str, command: dict[str, Any]) -> Released:
        if len(canonical_bytes(command)) > 131072 or not VALIDATOR.is_valid(command):
            raise LocalError("invalid_request")
        if command["incarnation"] != self.s.incarnation:
            raise LocalError("history_changed")
        action, args, receipt = command["action"], command["args"], uid("rcpt")
        with self.s.transaction() as decision:
            at = decision.evaluated_ms
            who = self.s._auth(credential, "read", args["project"], at)
            deps: list[dict[str, str]] = []
            if action == "extract":
                self.s._auth(credential, "capture", args["project"], at)
                result = self.extract(credential, who, command, at)
            elif action == "compile":
                if args["candidate"] is not None:
                    self.s._auth(credential, "inference", args["project"], at)
                raw, deps = self.compile(who, args, decision, receipt)
                result = json.loads(raw)
            else:
                self.s._auth(credential, "admin", args["project"], at)
                result = self.usage(who, args)
            if action != "compile":
                result.update(
                    receipt=receipt,
                    evaluated_ms=at,
                    deadline_ms=decision.deadline_ms,
                    incarnation=self.s.incarnation,
                )
                raw = canonical_bytes(result)
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
                    json.dumps([d["id"] for d in deps if d["kind"] == "observation"]),
                ),
            )
            self.s.fault("after_validation")
        self.s._expose()
        return Released(raw, decision, self.s.clock, receipt)

    def attest_usage(
        self,
        who: dict[str, Any],
        project: str,
        attempt: str,
        stage: str,
        provenance: dict[str, Any],
        at: int,
    ) -> str:
        identifier = canonical_hash([self.s.incarnation, who["principal"], project, stage, attempt])
        request_hash = canonical_hash(provenance)
        previous = self.db.execute(
            "SELECT request_hash FROM processing_usage WHERE tenant=? AND id=?",
            (who["tenant"], identifier),
        ).fetchone()
        if previous is not None:
            if previous["request_hash"] != request_hash:
                raise LocalError("usage_attempt_conflict")
            return identifier
        if (
            self.db.execute(
                "SELECT count(*) FROM processing_usage WHERE tenant=?", (who["tenant"],)
            ).fetchone()[0]
            >= USAGE_LIMIT
        ):
            # Only rows past their retention are released; recent attempts are never
            # silently discarded to make room.
            self.db.execute(
                "DELETE FROM processing_usage WHERE tenant=? AND expires<=?", (who["tenant"], at)
            )
            if (
                self.db.execute(
                    "SELECT count(*) FROM processing_usage WHERE tenant=?", (who["tenant"],)
                ).fetchone()[0]
                >= USAGE_LIMIT
            ):
                raise LocalError("usage_retention_limit")
        self.db.execute(
            "INSERT INTO processing_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                who["tenant"],
                identifier,
                project,
                who["principal"],
                self.s.incarnation,
                "attested:" + provenance["provider"],
                provenance["configuration_hash"],
                stage,
                "unknown",
                None,
                None,
                None,
                None,
                0,
                request_hash,
                provenance["output_hash"],
                "[]",
                at,
                at + RETRY_MS,
            ),
        )
        return identifier

    def extract(
        self, credential: str, who: dict[str, Any], command: dict[str, Any], at: int
    ) -> dict[str, Any]:
        args = command["args"]
        body, provenance = args["body"], args["provenance"]
        self.k._support(who, body["evidence"], at)
        # Blob identities plus exact byte ranges identify checked UTF-8 source bytes.
        if provenance["input_hash"] != canonical_hash(body["evidence"]):
            raise LocalError("source_manifest_mismatch")
        if provenance["output_hash"] != canonical_hash(body):
            raise LocalError("output_hash_mismatch")
        lookup = (
            who["tenant"],
            self.s.incarnation,
            "processing-v1",
            who["principal"],
            "extract",
            command["key"],
        )
        previous = self.db.execute(
            "SELECT * FROM retries WHERE tenant=? AND incarnation=? AND namespace=? AND principal=? AND operation=? AND key=?",
            lookup,
        ).fetchone()
        if previous is not None:
            if previous["hash"] != canonical_hash(command):
                raise LocalError("idempotency_conflict")
            if previous["result"] is None or at >= previous["expires"]:
                raise LocalError("idempotency_result_expired")
            return {**json.loads(previous["result"]), "replayed": True}
        usage = self.attest_usage(
            who, args["project"], command["key"], "capture_extraction", provenance, at
        )
        result = self.k.propose(
            credential,
            who,
            {
                **args,
                "body": {
                    **body,
                    "provenance": {
                        **provenance,
                        "assurance": "actor_reported",
                        "usage_attempt": usage,
                        "validation": "source_bytes_and_output_hash_only",
                        "semantic_entailment": "unverified",
                    },
                },
            },
            at,
        )
        result.update(usage_attempt=usage, assurance="actor_reported", automatic_acceptance=False)
        self.db.execute(
            "INSERT INTO retries VALUES (?,?,?,?,?,?,?,?,?,?)",
            (*lookup, canonical_hash(command), json.dumps(result), None, at + RETRY_MS),
        )
        return result

    def sources(self, who: dict[str, Any], args: dict[str, Any], at: int) -> list[dict[str, Any]]:
        units = []
        for identifier in args["sources"]:
            row = self.db.execute(
                "SELECT * FROM observations WHERE tenant=? AND id=?", (who["tenant"], identifier)
            ).fetchone()
            if (
                row is None
                or row["project"] != args["project"]
                or not self.s._visible(who, row, at)
            ):
                raise LocalError("optional_source_unavailable")
            raw = self.r._blob(row)
            if len(raw) > 8192:
                raise LocalError("optional_source_limit")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise LocalError("optional_source_not_text") from exc
            units.append(
                {
                    "observation": identifier,
                    "content_hash": row["blob"],
                    "start_byte": 0,
                    "end_byte": len(raw),
                    "text": text,
                }
            )
        return units

    def claims(self, who: dict[str, Any], args: dict[str, Any], at: int) -> list[dict[str, str]]:
        if not args["claims"]:
            return []
        query_args = {
            key: args[key]
            for key in ("project", "task", "run", "bindings")
            if args[key] is not None
        }
        current = self.k.query(who, query_args, at, constraints=False)
        if any(set(pair).intersection(args["claims"]) for pair in current.get("conflicts", [])):
            raise LocalError("optional_claim_conflict")
        eligible = {item["revision"]: item for item in current["items"]}
        selected = []
        for revision in args["claims"]:
            item = eligible.get(revision)
            if item is None or item["authority"] != "accepted_knowledge":
                raise LocalError("optional_claim_unavailable")
            claim = self.r._claim(item)
            if not {span["observation"] for span in claim["evidence"]}.issubset(args["sources"]):
                raise LocalError("optional_claim_sources_incomplete")
            selected.append(claim)
        return self.r._dependencies(selected)

    def compile(
        self, who: dict[str, Any], args: dict[str, Any], decision: Decision, receipt: str
    ) -> tuple[bytes, list[dict[str, str]]]:
        raw, deps = self.w._resume(who, args, decision, receipt, retain=False)
        packet, at = json.loads(raw), decision.evaluated_ms
        if packet["status"] in {"ready", "incomplete"}:
            try:
                units = self.sources(who, args, at)
                claim_deps = self.claims(who, args, at)
                summary = validate(units, packet, args["candidate"])
            except LocalError as exc:
                units, claim_deps = [], []
                summary = {"mode": "omitted", "reason": exc.code, "quotes": []}
            candidate = args["candidate"]
            if candidate is not None:
                summary["usage_attempt"] = self.attest_usage(
                    who,
                    args["project"],
                    candidate["attempt"],
                    "compression",
                    candidate["provenance"],
                    at,
                )
                summary["provenance"] = {**candidate["provenance"], "assurance": "actor_reported"}
            # Candidates cannot set any checkpoint, policy, operation, or recovery field.
            packet["optional_explanation"] = summary
            count, _ = counter(args["tokenizer"])
            candidate_raw, used = counted(packet, count)
            if used > args["max_output_tokens"] or len(candidate_raw) > 262144:
                packet["optional_explanation"] = {
                    "mode": "omitted",
                    "reason": "optional_budget",
                    "quotes": [],
                }
                candidate_raw, used = counted(packet, count)
                units, claim_deps = [], []
            if used <= args["max_output_tokens"] and len(candidate_raw) <= 262144:
                raw = candidate_raw
                deps.extend(claim_deps)
                deps.extend({"kind": "observation", "id": unit["observation"]} for unit in units)
            # If even the omission marker does not fit, retain the complete original.
        result = json.loads(raw)
        self.r._retain(
            who,
            {"project": args["project"], "action": "processing_compile"},
            receipt,
            raw,
            {
                "kind": "processing_compile",
                "status": result["status"],
                "trace": [],
                "usage": result["usage"],
                "optional": result.get("optional_explanation", {}).get("reason", "not_rendered"),
            },
            deps,
            at,
        )
        return raw, deps

    def usage(self, who: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        rows = self.db.execute(
            "SELECT * FROM processing_usage WHERE tenant=? AND project=? ORDER BY id LIMIT 100001",
            (who["tenant"], args["project"]),
        ).fetchall()
        records = [
            UsageRecord(
                row["id"],
                row["provider"],
                row["fingerprint"],
                row["stage"],
                row["status"],
                row["input_tokens"],
                row["cached_input_tokens"],
                row["output_tokens"],
                row["cost_microusd"],
                row["duration_ms"],
            )
            for row in rows
        ]
        return {
            **summarize(records, args["successful_tasks"]),
            "coverage": "retained_processing_records_only_not_complete_task_cost",
            "task_outcomes": "caller_reported_not_validated",
            "attested_duration": "not_observed_zero_placeholder",
            "history_incarnations": sorted({row["incarnation"] for row in rows}),
            "savings_qualification": "not_evaluated",
        }
