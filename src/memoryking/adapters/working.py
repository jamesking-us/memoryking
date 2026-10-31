"""Durable checkpoints and operation intent, sharing the local authorization/release gate."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator

from memoryking.adapters.local_store import (
    INTENT_LIMIT,
    INTENT_SCOPE_LIMIT,
    RETRY_MS,
    LocalStore,
    uid,
)
from memoryking.adapters.retrieval import Retrieval
from memoryking.adapters.token_count import counter
from memoryking.contracts.canonical import ContractError, canonical_bytes, canonical_hash
from memoryking.domain.briefing import counted
from memoryking.domain.checkpoint import dependencies, operation_transition, stream_name
from memoryking.domain.local import Decision, LocalError, Released

VALIDATOR = Draft202012Validator(
    json.loads(Path(__file__).with_name("working.schema.json").read_text())
)
MUTATIONS = {"save", "assign_writer", "operation_intent", "operation_transition"}


class Working:
    def __init__(self, store: LocalStore):
        self.s, self.db = store, store.db
        self.r = Retrieval(store)

    def _scope(self, who: dict[str, Any], args: dict[str, Any]) -> tuple[str, str, str, str]:
        return who["tenant"], args["project"], args["task"], stream_name(args["run"])

    def _stream(self, who: dict[str, Any], args: dict[str, Any]) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            self.db.execute(
                "SELECT * FROM checkpoint_streams WHERE tenant=? AND project=? AND task=? AND stream=?",
                self._scope(who, args),
            ).fetchone(),
        )

    def _writer(self, who: dict[str, Any], args: dict[str, Any], *, create: bool = False) -> None:
        row = self._stream(who, args)
        if row is None:
            if create:
                return
            raise LocalError("checkpoint_unavailable")
        if row["incarnation"] != self.s.incarnation:
            raise LocalError("checkpoint_reconciliation_required")
        if row["writer"] != who["principal"]:
            raise LocalError("checkpoint_writer_denied")

    def _dependency(self, who: dict[str, Any], dep: dict[str, str], at: int) -> None:
        try:
            self.r._dependency(who, dep, at)
        except LocalError as exc:
            raise LocalError("checkpoint_evidence_unavailable") from exc

    def _body(self, who: dict[str, Any], body: dict[str, Any], at: int) -> list[dict[str, str]]:
        deps = dependencies(body)
        for dep in deps:
            self._dependency(who, dep, at)
        for artifact in body["repository"]["artifacts"]:
            path = artifact["path"]
            if (
                path.startswith(("/", "\\"))
                or "\\" in path
                or any(p in {"", ".", ".."} for p in path.split("/"))
            ):
                raise LocalError("artifact_path_invalid")
            if artifact["availability"] == "stored":
                row = self.db.execute(
                    "SELECT * FROM observations WHERE tenant=? AND id=?",
                    (who["tenant"], artifact["observation"]),
                ).fetchone()
                if row is None or row["blob"] != artifact["content_hash"]:
                    raise LocalError("artifact_integrity_failure")
                # A matching manifest alone does not establish recoverable bytes.
                try:
                    self.r._blob(row)
                except (LocalError, OSError) as exc:
                    raise LocalError("artifact_unavailable") from exc
        return deps

    def _operation(self, who: dict[str, Any], project: str, operation: str) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT * FROM operation_intents WHERE tenant=? AND id=? AND project=? AND principal=?",
            (who["tenant"], operation, project, who["principal"]),
        ).fetchone()
        if row is None:
            raise LocalError("operation_unavailable")
        return cast(sqlite3.Row, row)

    def _preflight(
        self, credential: str, who: dict[str, Any], action: str, args: dict[str, Any], at: int
    ) -> None:
        if action == "save":
            self._writer(who, args, create=True)
            if who["principal"] not in args["readers"]:
                raise LocalError("checkpoint_writer_audience_required")
            self._body(who, args["checkpoint"], at)
            for operation in args["checkpoint"]["operation_ids"]:
                row = self.db.execute(
                    "SELECT * FROM operation_intents WHERE tenant=? AND project=? AND task=? AND stream=? AND id=?",
                    (*self._scope(who, args), operation),
                ).fetchone()
                if row is None:
                    raise LocalError("operation_unavailable")
                for dep in json.loads(row["evidence"]):
                    self._dependency(who, dep, at)
        elif action == "assign_writer":
            self.s._auth(credential, "admin", "*", at)
            token = self.s._token("credential", tenant=who["tenant"], principal=args["writer"])
            self.s._subject_auth(token, "read", args["project"], at)
            self.s._subject_auth(token, "checkpoint", args["project"], at)
        elif action == "operation_intent":
            self._writer(who, args)
            if (args["method"] == "idempotent_retry") != (args["provider_key"] is not None):
                raise LocalError("operation_reconciliation_invalid")
        elif action == "operation_transition":
            row = self._operation(who, args["project"], args["operation"])
            if args["state"] == "dispatching":
                self._writer(
                    who,
                    {
                        "project": row["project"],
                        "task": row["task"],
                        "run": None if row["stream"] == "task" else row["stream"][4:],
                    },
                )
                if row["incarnation"] != self.s.incarnation:
                    raise LocalError("operation_reconciliation_required")
            for dep in args["evidence"]:
                self._dependency(who, dep, at)
            if args["state"] in {"succeeded", "failed"}:
                if not args["evidence"] or args["result_hash"] is None:
                    raise LocalError("operation_result_evidence_required")
            elif args["evidence"] or args["result_hash"] is not None:
                raise LocalError("operation_result_invalid")

    def execute(self, credential: str, command: dict[str, Any]) -> Released:
        if len(canonical_bytes(command)) > 131072 or not VALIDATOR.is_valid(command):
            raise LocalError("invalid_request")
        if command["incarnation"] != self.s.incarnation:
            raise LocalError("history_changed")
        action, args = command["action"], command["args"]
        receipt = uid("rcpt")
        deps: list[dict[str, str]] = []
        with self.s.transaction() as decision:
            at = decision.evaluated_ms
            who = self.s._auth(
                credential, "checkpoint" if action in MUTATIONS else "read", args["project"], at
            )
            self.s._auth(credential, "read", args["project"], at)
            self.r._expire(at)
            self._preflight(credential, who, action, args, at)
            key = command.get("key")
            lookup = (
                who["tenant"],
                self.s.incarnation,
                "working-v1",
                who["principal"],
                action,
                key,
            )
            old = None
            if action in MUTATIONS:
                old = self.db.execute(
                    "SELECT * FROM retries WHERE tenant=? AND incarnation=? AND namespace=? AND principal=? AND operation=? AND key=?",
                    lookup,
                ).fetchone()
            if old is not None:
                if old["hash"] != canonical_hash(command):
                    raise LocalError("idempotency_conflict")
                if old["result"] is None or at >= old["expires"]:
                    raise LocalError("idempotency_result_expired")
                result = json.loads(old["result"])
            elif action == "save":
                result = self._save(who, args, at)
            elif action == "assign_writer":
                result = self._assign(who, args, at)
            elif action.startswith("operation_"):
                result = self._write_operation(who, command, at)
            elif action == "operations":
                result = {"operations": self._operations(who, args, at)[0]}
            elif action == "adapter_resume":
                from memoryking.adapters.adapter_state import resume

                raw, deps = resume(self, who, args, decision, receipt)
                result = json.loads(raw)
            else:
                raw, deps = self._resume(who, args, decision, receipt)
                result = json.loads(raw)
            if action in MUTATIONS and old is None and not action.startswith("operation_"):
                self.db.execute(
                    "INSERT INTO retries VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (*lookup, canonical_hash(command), json.dumps(result), None, at + RETRY_MS),
                )
            if action not in {"resume", "adapter_resume"}:
                if action in MUTATIONS:
                    result["replayed"] = old is not None
                result.update(
                    receipt=receipt,
                    incarnation=self.s.incarnation,
                    evaluated_ms=at,
                    deadline_ms=decision.deadline_ms,
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
                    hashlib.sha256(raw).hexdigest(),
                    json.dumps([d["id"] for d in deps if d["kind"] == "observation"]),
                ),
            )
            self.s.fault("after_validation")
        self.s._expose()
        return Released(raw, decision, self.s.clock, receipt)

    def _save(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        scope = self._scope(who, args)
        stream = self._stream(who, args)
        current = stream["revision"] if stream is not None else 0
        # None opts out of compare-and-set: the caller accepts the current revision.
        if args["expected_revision"] is not None and args["expected_revision"] != current:
            raise LocalError("checkpoint_revision_conflict")
        total, size = self.db.execute(
            "SELECT count(*),coalesce(sum(length(CAST(body AS BLOB))),0) FROM checkpoints WHERE tenant=?",
            (who["tenant"],),
        ).fetchone()
        body = canonical_bytes(args["checkpoint"])
        if total >= 4096 or size + len(body) > 268435456:
            raise LocalError("checkpoint_capacity")
        identifier, digest = uid("ckpt"), hashlib.sha256(body).hexdigest()
        position = self.s._event(
            who["tenant"], "checkpoint_saved", identifier, at, who["principal"]
        )
        deps = dependencies(args["checkpoint"])
        self.db.execute(
            "INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                who["tenant"],
                identifier,
                *scope[1:],
                current + 1,
                who["principal"],
                self.s.incarnation,
                at,
                body.decode(),
                digest,
                json.dumps(args["readers"]),
                json.dumps(deps),
                "available",
                position,
            ),
        )
        self.db.execute(
            "INSERT INTO checkpoint_streams VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(tenant,project,task,stream) DO UPDATE SET revision=excluded.revision,latest=excluded.latest",
            (*scope, who["principal"], current + 1, self.s.incarnation, identifier),
        )
        self.s._enqueue(who["tenant"], position, None, who["principal"])
        return {
            "checkpoint": identifier,
            "revision": current + 1,
            "body_hash": digest,
            "status": "durable",
            "source_position": position,
        }

    def _assign(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        row = self._stream(who, args)
        if row is None:
            raise LocalError("checkpoint_unavailable")
        if args["expected_revision"] != row["revision"]:
            raise LocalError("checkpoint_revision_conflict")
        # The owner explicitly reconciles ownership after history replacement.
        self.db.execute(
            "UPDATE checkpoint_streams SET writer=?,revision=revision+1,incarnation=? WHERE tenant=? AND project=? AND task=? AND stream=?",
            (args["writer"], self.s.incarnation, *self._scope(who, args)),
        )
        position = self.s._event(
            who["tenant"], "checkpoint_writer_assigned", args["task"], at, who["principal"]
        )
        self.s._enqueue(who["tenant"], position, None, who["principal"])
        return {"revision": row["revision"] + 1, "writer": args["writer"], "reason": args["reason"]}

    def _checkpoint(self, who: dict[str, Any], args: dict[str, Any], at: int) -> sqlite3.Row:
        identifier = args["checkpoint"]
        if identifier is None:
            stream = self._stream(who, args)
            identifier = stream["latest"] if stream is not None else None
        row = self.db.execute(
            "SELECT * FROM checkpoints WHERE tenant=? AND id=? AND project=? AND task=? AND stream=?",
            (who["tenant"], identifier, *self._scope(who, args)[1:]),
        ).fetchone()
        if row is None or who["principal"] not in json.loads(row["readers"]):
            raise LocalError("checkpoint_unavailable")
        if row["incarnation"] != self.s.incarnation:
            raise LocalError("history_changed")
        if row["state"] != "available" or row["body"] is None:
            raise LocalError("checkpoint_unavailable")
        if hashlib.sha256(row["body"].encode()).hexdigest() != row["body_hash"]:
            raise LocalError("checkpoint_integrity_failure")
        for dep in json.loads(row["dependencies"]):
            self._dependency(who, dep, at)
        return cast(sqlite3.Row, row)

    def _operations(
        self, who: dict[str, Any], args: dict[str, Any], at: int, *, include_all: bool = False
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        stream = self._stream(who, args)
        all_writers = include_all or (
            stream is not None
            and stream["writer"] == who["principal"]
            and stream["incarnation"] == self.s.incarnation
        )
        rows = self.db.execute(
            "SELECT * FROM operation_intents WHERE tenant=? AND project=? AND task=? AND stream=?"
            + ("" if all_writers else " AND principal=?")
            + " ORDER BY created,id LIMIT 33",
            self._scope(who, args) if all_writers else (*self._scope(who, args), who["principal"]),
        ).fetchall()
        if len(rows) > 32:
            raise LocalError("operation_limit")
        result, deps = [], []
        for row in rows:
            evidence = json.loads(row["evidence"])
            evidence_available = True
            try:
                for dep in evidence:
                    self._dependency(who, dep, at)
            except LocalError:
                # The originating actor may inspect safe operation identity after source loss.
                # Other checkpoint readers do not gain a new disclosure path.
                if row["principal"] != who["principal"]:
                    raise
                evidence, evidence_available = [], False
            deps.extend(evidence)
            result.append(
                {
                    k: row[k]
                    for k in (
                        "id",
                        "principal",
                        "target_hash",
                        "parameters_hash",
                        "method",
                        "provider_key",
                        "state",
                        "revision",
                        "result_hash",
                        "incarnation",
                    )
                }
            )
            result[-1].update(
                evidence=evidence,
                evidence_available=evidence_available,
                reconciled_incarnation=row["reconciled_incarnation"],
                reconciliation_required=not evidence_available
                or row["state"] in {"dispatching", "unknown"}
                or (row["reconciled_incarnation"] or row["incarnation"]) != self.s.incarnation,
            )
            if not evidence_available:
                result[-1]["result_hash"] = None
        return result, deps

    def _resume(
        self,
        who: dict[str, Any],
        args: dict[str, Any],
        decision: Decision,
        receipt: str,
        *,
        extras: dict[str, Any] | None = None,
        retain: bool = True,
    ) -> tuple[bytes, list[dict[str, str]]]:
        at = decision.evaluated_ms
        row = self._checkpoint(who, args, at)
        body = json.loads(row["body"])
        deps = json.loads(row["dependencies"])
        count, usage = counter(args["tokenizer"])
        policy_args = {
            k: args[k] for k in ("project", "task", "run", "bindings") if args[k] is not None
        }
        mandatory = self.r.k.query(who, policy_args, at, constraints=True)
        repository = body["repository"]
        repository_snapshot = None
        if repository.get("snapshot_id"):
            from memoryking.adapters.repository_store import Repository

            try:
                repo = Repository(self.s)
                repo.visible(who, repository["repository"], args["project"])
                repository_snapshot = json.loads(
                    repo.snapshot(
                        who["tenant"], repository["repository"], repository["snapshot_id"]
                    )["body"]
                )
                if (
                    repository.get("current_commit") != repository_snapshot["commit"]
                    or repository.get("environment") != repository_snapshot["environment"]
                    or repository["dirty"] != repository_snapshot["dirty"]
                    or set(repository.get("deleted_paths", []))
                    != {
                        p
                        for p in repository_snapshot["dirty_paths"]
                        if repository_snapshot["files"][p]["coverage"] == "missing"
                    }
                ):
                    raise LocalError("repository_context_mismatch")
                coverage = repo.coverage(
                    who,
                    {
                        "repository": repository["repository"],
                        "snapshot": repository["snapshot_id"],
                        "paths": repository.get("change_scope", []),
                        "bindings": args["bindings"],
                    },
                )
                if coverage["status"] != "complete":
                    mandatory = {
                        "status": "blocked",
                        "reason": "binding_coverage_incomplete",
                        "items": [],
                    }
            except (LocalError, TypeError):
                mandatory = {
                    "status": "blocked",
                    "reason": "repository_context_unavailable",
                    "items": [],
                }
        base = self.r._base(args, decision, receipt, usage)
        base.update(
            status="ready",
            checkpoint=None,
            operations=[],
            context_binding={
                "store": self.s.store_id,
                "tenant": who["tenant"],
                "principal": who["principal"],
                "incarnation": self.s.incarnation,
                "policy_epoch": self.db.execute(
                    "SELECT policy_epoch FROM tenants WHERE tenant=?", (who["tenant"],)
                ).fetchone()[0],
            },
        )
        if mandatory["status"] != "complete":
            base.update(status="blocked", reason=mandatory["reason"])
            deps = []
        else:
            operations, operation_deps = self._operations(who, args, at, include_all=True)
            deps.extend(operation_deps)
            base.update(
                checkpoint={
                    "id": row["id"],
                    "revision": row["revision"],
                    "writer": row["writer"],
                    "created_ms": row["created"],
                    "body_hash": row["body_hash"],
                    "compiler_revision": "checkpoint-v1",
                    "state": body,
                },
                operations=operations,
                constraints=[self.r._claim(i) for i in mandatory["items"]],
            )
            deps.extend(self.r._dependencies(base["constraints"]))
            gaps = list(body["capture"]["gaps"])
            if repository_snapshot is not None:
                artifacts = {a["path"]: a for a in repository["artifacts"]}
                for path in repository_snapshot["dirty_paths"]:
                    file = repository_snapshot["files"][path]
                    if file["coverage"] == "missing" and path in repository.get(
                        "deleted_paths", []
                    ):
                        continue
                    artifact = artifacts.get(path)
                    if (
                        file["hash"] is None
                        or artifact is None
                        or artifact["availability"] != "stored"
                        or artifact["content_hash"] != file["hash"]
                    ):
                        gaps.append("dirty_snapshot_artifact_unavailable")
            if (
                body["repository"]["dirty"]
                and not body["repository"]["artifacts"]
                and not (repository_snapshot is not None and repository.get("deleted_paths"))
            ):
                gaps.append("dirty_state_without_artifacts")
            for artifact in body["repository"]["artifacts"]:
                if artifact["required"] and artifact["availability"] != "stored":
                    gaps.append("required_artifact_not_durable")
                elif artifact["availability"] == "stored":
                    try:
                        self.r._blob(
                            self.db.execute(
                                "SELECT * FROM observations WHERE tenant=? AND id=?",
                                (who["tenant"], artifact["observation"]),
                            ).fetchone()
                        )
                    except (LocalError, OSError):
                        gaps.append("stored_artifact_unavailable")
            if gaps or any(op["reconciliation_required"] for op in operations):
                base.update(
                    status="incomplete",
                    recovery_gaps=sorted(set(gaps)),
                    reason="recovery_reconciliation_required",
                )
        if extras:
            base.update(extras)
        try:
            raw, used = counted(base, count)
            minimum: int | None = used
        except ContractError:
            raw, used, minimum = b"", 0, None
        if not raw or used > args["max_output_tokens"] or len(raw) > 262144:
            base.update(
                status="budget_insufficient",
                reason="essential_set_does_not_fit" if raw else "essential_serialization_limit",
                checkpoint=None,
                operations=[],
                constraints=[],
                conflicts=[],
                minimum_required_tokens=minimum,
            )
            base.pop("recovery_gaps", None)
            base["usage"]["budget_compliant"] = False
            raw, used = counted(base, count)
            deps = []
        result = json.loads(raw)
        diag = {
            "status": result["status"],
            "kind": "checkpoint_resume",
            "checkpoint": row["id"],
            "checkpoint_hash": row["body_hash"],
            "usage": {**result["usage"], "output_tokens": used, "serialized_bytes": len(raw)},
            "minimum_required_tokens": result.get("minimum_required_tokens"),
            "budget_count_scope": "failure_envelope"
            if result["status"] == "budget_insufficient"
            else "context",
            "trace": [],
            "context_binding": base["context_binding"],
        }
        # Receipt replay must recheck the checkpoint audience/history as well as source closure.
        if result["checkpoint"] is not None:
            deps.append({"kind": "checkpoint", "id": row["id"]})
        if retain:
            self.r._retain(who, args, receipt, raw, diag, deps, at)
        return raw, deps

    def _write_operation(
        self, who: dict[str, Any], command: dict[str, Any], at: int
    ) -> dict[str, Any]:
        action, args = command["action"], command["args"]
        if action == "operation_intent":
            if self.db.execute(
                "SELECT 1 FROM operation_intents WHERE tenant=? AND id=?",
                (who["tenant"], args["operation"]),
            ).fetchone():
                raise LocalError("operation_identity_consumed")
            # Resolved intents keep their consumed identity but release capacity.
            if (
                self.db.execute(
                    "SELECT count(*) FROM operation_intents WHERE tenant=? AND state IN ('intent_recorded','dispatching','unknown')",
                    (who["tenant"],),
                ).fetchone()[0]
                >= INTENT_LIMIT
            ):
                raise LocalError("operation_capacity")
            if (
                self.db.execute(
                    "SELECT count(*) FROM operation_intents WHERE tenant=? AND project=? AND task=? AND stream=? AND state IN ('intent_recorded','dispatching','unknown')",
                    self._scope(who, args),
                ).fetchone()[0]
                >= INTENT_SCOPE_LIMIT
            ):
                raise LocalError("operation_scope_capacity")
            record = {
                "id": args["operation"],
                "principal": who["principal"],
                "project": args["project"],
                "task": args["task"],
                "stream": stream_name(args["run"]),
                "incarnation": self.s.incarnation,
                "target_hash": args["target_hash"],
                "parameters_hash": args["parameters_hash"],
                "method": args["method"],
                "provider_key": args["provider_key"],
                "state": "intent_recorded",
                "revision": 1,
                "result_hash": None,
                "evidence": [],
                "created": at,
                "updated": at,
            }
            record["reconciled_incarnation"] = None
        else:
            row = self._operation(who, args["project"], args["operation"])
            if row["revision"] != args["expected_revision"]:
                raise LocalError("operation_revision_conflict")
            if (row["reconciled_incarnation"] or row["incarnation"]) != self.s.incarnation:
                if args["state"] not in {"succeeded", "failed"} or not args["reconciled"]:
                    raise LocalError("operation_reconciliation_required")
            else:
                operation_transition(row["state"], args["state"], args["reconciled"])
            record = dict(row)
            record.pop("tenant")
            record.update(
                state=args["state"],
                revision=row["revision"] + 1,
                result_hash=args["result_hash"],
                evidence=args["evidence"],
                updated=at,
            )
            if args["reconciled"]:
                record["reconciled_incarnation"] = self.s.incarnation
        # Safe hashes and IDs only: no external parameters, returned text or credentials.
        journal = self.s._journal(
            {
                "kind": "operation",
                "tenant": who["tenant"],
                "actor": who["principal"],
                "operation": record["id"],
                "record": record,
                "request": {
                    "incarnation": self.s.incarnation,
                    "action": action,
                    "key": command["key"],
                    "hash": canonical_hash(command),
                    "expires": at + RETRY_MS,
                },
            }
        )
        self.s._apply(journal, at)
        return {"operation": record["id"], "state": record["state"], "revision": record["revision"]}

    def apply_operation(self, action: dict[str, Any]) -> None:
        """Replay independently retained operation evidence even after an older content restore."""
        tenant, row, request = action["tenant"], action["record"], action["request"]
        old = self.db.execute(
            "SELECT revision FROM operation_intents WHERE tenant=? AND id=?", (tenant, row["id"])
        ).fetchone()
        if old is None or old["revision"] < row["revision"]:
            values = (
                tenant,
                *(
                    row[k]
                    for k in (
                        "id",
                        "principal",
                        "project",
                        "task",
                        "stream",
                        "incarnation",
                        "target_hash",
                        "parameters_hash",
                        "method",
                        "provider_key",
                        "state",
                        "revision",
                        "result_hash",
                    )
                ),
                json.dumps(row["evidence"]),
                row["created"],
                row["updated"],
                row["reconciled_incarnation"],
            )
            self.db.execute(
                "INSERT INTO operation_intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(tenant,id) DO UPDATE SET state=excluded.state,revision=excluded.revision,result_hash=excluded.result_hash,evidence=excluded.evidence,updated=excluded.updated,reconciled_incarnation=excluded.reconciled_incarnation",
                values,
            )
        result = {"operation": row["id"], "state": row["state"], "revision": row["revision"]}
        self.db.execute(
            "INSERT OR IGNORE INTO retries VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                tenant,
                request["incarnation"],
                "working-v1",
                action["actor"],
                request["action"],
                request["key"],
                request["hash"],
                json.dumps(result),
                None,
                request["expires"],
            ),
        )
