"""SQLite evidence-backed claims and complete policy registry, under the local gate."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator

from memoryking.adapters.local_files import private_file
from memoryking.adapters.local_store import RETRY_MS, uid
from memoryking.application.storage import insert_ignore
from memoryking.application.store_core import StoreCore
from memoryking.contracts.canonical import canonical_bytes, canonical_hash
from memoryking.domain import selectors
from memoryking.domain.local import LocalError, Released, temporal_valid

REGISTRY_LIMIT = 256
VALIDATOR = Draft202012Validator(
    json.loads(Path(__file__).with_name("knowledge.schema.json").read_text())
)


class Knowledge:
    def __init__(self, store: StoreCore):
        self.s = store
        self.db = store.db

    def execute(self, credential: str, command: dict[str, Any]) -> Released:
        canonical_bytes(command)
        if not VALIDATOR.is_valid(command):
            raise LocalError("invalid_request")
        if command["incarnation"] != self.s.incarnation:
            raise LocalError("history_changed")
        action, args = command["action"], command["args"]
        cap = {
            "propose": "capture",
            "review": "review",
            "attest": "attest",
            "promote": "review",
        }.get(action, "read")
        with self.s.transaction() as decision:
            who = self.s._auth(credential, cap, args["project"], decision.evaluated_ms)
            at = decision.evaluated_ms
            if args.get("diagnostic", False):
                self.s._auth(credential, "review", args["project"], at)
            # Required operation capabilities apply equally to fresh calls and retries.
            if action == "propose" and args["body"]["kind"] == "constraint":
                self.s._auth(
                    credential,
                    "task_constraint" if args["body"]["authority"] == "task" else "policy",
                    args["project"],
                    at,
                )
            if action == "promote":
                self.s._auth(credential, "capture", args["project"], at)
                source = self._revision(who["tenant"], args["revision"])
                self.s._auth(credential, "review", source["project"], at)
                self.s._auth(credential, "export", source["project"], at)
            mutation = action in {"propose", "review", "attest", "promote"}
            key = command.get("key")
            if mutation and not key:
                raise LocalError("request_key_required")
            existing = None
            lookup = (
                who["tenant"],
                self.s.incarnation,
                "knowledge-v1",
                who["principal"],
                action,
                key,
            )
            if mutation:
                existing = self.db.execute(
                    "SELECT * FROM retries WHERE tenant=? AND incarnation=? AND namespace=? AND principal=? AND operation=? AND key=?",
                    lookup,
                ).fetchone()
            if existing is not None:
                if existing["hash"] != canonical_hash(command):
                    raise LocalError("idempotency_conflict")
                if existing["result"] is None or at >= existing["expires"]:
                    raise LocalError("idempotency_result_expired")
                result = json.loads(existing["result"])
                # Replay contains IDs only, but the operation's present authority is required.
                if action == "review":
                    self._authority(
                        credential,
                        self._revision(who["tenant"], args["revision"], args["project"]),
                        at,
                    )
            elif action == "propose":
                result = self.propose(credential, who, args, at)
            elif action == "promote":
                result = self.promote(credential, who, args, at)
            elif action == "review":
                result = self.review(credential, who, args, at)
            elif action == "attest":
                result = self.attest(who, args, at)
            else:
                result = self.query(who, args, at, constraints=action == "constraints")
            if mutation and existing is None:
                self.db.execute(
                    "INSERT INTO retries VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (*lookup, canonical_hash(command), json.dumps(result), None, at + RETRY_MS),
                )
            if len(json.dumps(result).encode()) > 524288:
                result = {"status": "blocked", "reason": "response_limit_exceeded", "items": []}
            result = {
                **result,
                "incarnation": self.s.incarnation,
                "evaluated_ms": at,
                "deadline_ms": decision.deadline_ms,
                "receipt": uid("rcpt"),
            }
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
                    canonical_hash(result),
                    "[]",
                ),
            )
            self.s.fault("after_validation")
        self.s._expose()
        return Released(canonical_bytes(result), decision, self.s.clock)

    def _revision(self, tenant: str, revision: str, project: str | None = None) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT r.*,f.project,f.version AS family_version FROM revisions r JOIN families f ON r.tenant=f.tenant AND r.family=f.id WHERE r.tenant=? AND r.id=?",
            (tenant, revision),
        ).fetchone()
        if row is None or (project is not None and row["project"] != project):
            raise LocalError("unavailable")
        return cast(sqlite3.Row, row)

    def _support(self, who: dict[str, Any], evidence: list[dict[str, Any]], at: int) -> None:
        for span in evidence:
            row = self.db.execute(
                "SELECT * FROM observations WHERE tenant=? AND id=?",
                (who["tenant"], span["observation"]),
            ).fetchone()
            if row is None or not self.s._visible(who, row, at):
                raise LocalError("evidence_unavailable")
            path = self.s.root / "blobs" / who["tenant"] / row["blob"]
            private_file(path)
            raw = path.read_bytes()
            if (
                hashlib.sha256(raw).hexdigest() != row["blob"]
                or span["content_hash"] != row["blob"]
            ):
                raise LocalError("evidence_changed")
            start, end = span["start_byte"], span["end_byte"]
            if not 0 <= start < end <= len(raw):
                raise LocalError("invalid_evidence_span")
            try:
                raw[:start].decode()
                raw[start:end].decode()
            except UnicodeDecodeError as exc:
                raise LocalError("invalid_evidence_span") from exc

    def _evidence(self, tenant: str, revision: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT observation,start_byte,end_byte,content_hash FROM evidence_spans WHERE tenant=? AND revision=?",
                (tenant, revision),
            )
        ]

    def _available(self, who: dict[str, Any], row: sqlite3.Row, at: int) -> bool:
        if row["purged"] or row["body"] is None:
            return False
        body = json.loads(row["body"])
        if body["readers"] is not None and who["principal"] not in body["readers"]:
            return False
        try:
            self.s._subject_auth(
                self.s._token("credential", tenant=who["tenant"], principal=who["principal"]),
                "read",
                row["project"],
                at,
            )
            self._support(who, self._evidence(who["tenant"], row["id"]), at)
        except LocalError:
            return False
        return True

    def _authority(self, credential: str, row: sqlite3.Row, at: int) -> None:
        if row["body"] is None:
            self.s._auth(
                credential,
                "policy" if row["kind"] == "constraint" else "review",
                row["project"],
                at,
            )
            return
        body = json.loads(row["body"])
        if row["kind"] == "constraint":
            cap = "task_constraint" if body["authority"] == "task" else "policy"
            who = self.s._auth(credential, cap, row["project"], at)
            if cap == "task_constraint" and who["principal"] != row["issuer"]:
                self.s._auth(credential, "policy", row["project"], at)

    def propose(
        self, credential: str, who: dict[str, Any], args: dict[str, Any], at: int
    ) -> dict[str, Any]:
        tenant, body = who["tenant"], dict(args["body"])
        if (
            body["valid_start"] is not None
            and body["valid_end"] is not None
            and body["valid_start"] >= body["valid_end"]
        ):
            raise LocalError("invalid_interval")
        if body["run"] and not body["task"]:
            raise LocalError("invalid_scope")
        if args["project"] in {"_tenant", "_workspace"}:
            self.s._auth(credential, "admin", "*", at)
            if body["kind"] != "constraint" or body["task"] or body["run"]:
                raise LocalError("invalid_scope")
        if body["kind"] == "constraint":
            if body["authority"] not in {"project", "task"}:
                raise LocalError("invalid_constraint")
            self.s._auth(
                credential,
                "task_constraint" if body["authority"] == "task" else "policy",
                args["project"],
                at,
            )
            if body["authority"] == "task" and not body["task"]:
                raise LocalError("invalid_scope")
            if body["rule_key"] is None or body["effect"] is None or body["rule_value"] is None:
                raise LocalError("invalid_constraint")
        elif any(body[k] is not None for k in ("authority", "rule_key", "effect", "rule_value")):
            raise LocalError("invalid_constraint")
        try:
            body["selector"] = selectors.canonical(body["selector"])
            executable = True
        except LocalError as exc:
            if exc.code not in {"selector_unsupported", "selector_empty"} or not args.get(
                "preserve_legacy", False
            ):
                raise
            executable = False
        self._support(who, body["evidence"], at)
        row = self.db.execute(
            "SELECT * FROM families WHERE tenant=? AND project=? AND logical_key=?",
            (tenant, args["project"], args["logical_key"]),
        ).fetchone()
        if row is not None:
            prior = self.db.execute(
                "SELECT kind FROM revisions WHERE tenant=? AND family=? LIMIT 1",
                (tenant, row["id"]),
            ).fetchone()
            if prior is not None and prior[0] != body["kind"]:
                raise LocalError("family_kind_mismatch")
        version = row["version"] if row else 0
        if args["expected_version"] != version:
            raise LocalError("family_version_conflict")
        family = row["id"] if row else uid("family")
        if row is None:
            self.db.execute(
                "INSERT INTO families VALUES (?,?,?,?,0)",
                (tenant, family, args["project"], args["logical_key"]),
            )
        revision = uid("rev")
        position = self.s._event(tenant, "propose", revision, at, who["principal"])
        body["executable"] = executable
        self.db.execute(
            "INSERT INTO revisions VALUES (?,?,?,?,?,?,?,?,?,0)",
            (
                tenant,
                revision,
                family,
                json.dumps(body),
                canonical_hash(body),
                body["kind"],
                who["principal"],
                position,
                self.s.incarnation,
            ),
        )
        for span in body["evidence"]:
            self.db.execute(
                "INSERT INTO evidence_spans VALUES (?,?,?,?,?,?)",
                (
                    tenant,
                    revision,
                    span["observation"],
                    span["start_byte"],
                    span["end_byte"],
                    span["content_hash"],
                ),
            )
        self.db.execute(
            "UPDATE families SET version=version+1 WHERE tenant=? AND id=?", (tenant, family)
        )
        self.s._enqueue(tenant, position, None, who["principal"])
        return {
            "revision": revision,
            "family": family,
            "family_version": version + 1,
            "position": position,
            "state": "proposed",
            "executable": executable,
        }

    def _state(self, tenant: str, revision: str, position: int) -> tuple[str, bool]:
        state, quarantine = "proposed", False
        for row in self.db.execute(
            "SELECT action FROM lifecycle WHERE tenant=? AND revision=? AND position<=? ORDER BY position",
            (tenant, revision, position),
        ):
            if row[0] == "quarantine":
                quarantine = True
            elif row[0] == "release_quarantine":
                quarantine = False
            else:
                state = {"accept": "accepted", "reject": "rejected", "deprecate": "deprecated"}.get(
                    row[0], state
                )
        return state, quarantine

    def review(
        self,
        credential: str,
        who: dict[str, Any],
        args: dict[str, Any],
        at: int,
        *,
        procedure: bool = False,
    ) -> dict[str, Any]:
        tenant = who["tenant"]
        row = self._revision(tenant, args["revision"], args["project"])
        if (row["kind"] == "procedure") != procedure:
            raise LocalError("procedure_review_required")
        if row["family_version"] != args["expected_version"]:
            raise LocalError("family_version_conflict")
        self._authority(credential, row, at)
        if not self._available(who, row, at) and not (
            row["purged"] and args["decision"] == "deprecate"
        ):
            raise LocalError("evidence_unavailable")
        action, body = args["decision"], json.loads(row["body"]) if row["body"] else {}
        state, quarantine = self._state(tenant, row["id"], 9_007_199_254_740_991)
        if action in {"accept", "release_quarantine"}:
            if not body["executable"]:
                raise LocalError("selector_unsupported")
            selectors.canonical(body["selector"])
            if not args["breadth_reviewed"]:
                raise LocalError("breadth_review_required")
        if (
            (action in {"accept", "reject"} and state != "proposed")
            or (action == "accept" and quarantine)
            or (action == "deprecate" and state != "accepted")
            or (action == "release_quarantine" and not quarantine)
        ):
            raise LocalError("invalid_transition")
        replacement = args.get("replacement")
        if replacement and action != "accept":
            raise LocalError("invalid_transition")
        if replacement:
            predecessor = self._revision(tenant, replacement["predecessor"], args["project"])
            if predecessor["family"] != row["family"] or predecessor["id"] == row["id"]:
                raise LocalError("unrelated_replacement")
            if self._state(tenant, predecessor["id"], 9_007_199_254_740_991)[0] != "accepted":
                raise LocalError("invalid_transition")
            if not self._available(who, predecessor, at):
                raise LocalError("evidence_unavailable")
            previous = json.loads(predecessor["body"])
            self._authority(credential, predecessor, at)
            if any(previous[k] != body[k] for k in ("kind", "authority", "task", "run")):
                raise LocalError("unrelated_replacement")
            replacement = dict(replacement)
            replacement["selector"] = selectors.canonical(replacement["selector"])
            for candidate in (body, previous):
                proof = selectors.contains(replacement["selector"], candidate["selector"])
                if proof != "proven":
                    raise LocalError(
                        "replacement_out_of_range"
                        if proof == "disproven"
                        else "selector_relation_indeterminate"
                    )
                start, end = replacement["valid_start"], replacement["valid_end"]
                if (
                    (start is not None and end is not None and start >= end)
                    or (
                        candidate["valid_start"] is not None
                        and (start is None or start < candidate["valid_start"])
                    )
                    or (
                        candidate["valid_end"] is not None
                        and (end is None or end > candidate["valid_end"])
                    )
                ):
                    raise LocalError("replacement_out_of_range")
        # Accepting only proposed successors makes replacement edges acyclic.
        journal = {
            "kind": "knowledge",
            "tenant": tenant,
            "actor": who["principal"],
            "revision": row["id"],
            "decision": action,
            "reason": args["reason"],
            "replacement": replacement,
            "expected_hash": row["body_hash"],
        }
        self.s._apply(self.s._journal(journal), at)
        return {
            "revision": row["id"],
            "family": row["family"],
            "family_version": row["family_version"] + 1,
            "state": action,
        }

    def apply_policy(self, action: dict[str, Any], at: int) -> None:
        tenant = action["tenant"]
        row = self.db.execute(
            "SELECT * FROM revisions WHERE tenant=? AND id=?", (tenant, action["revision"])
        ).fetchone()
        replacement = action["replacement"]
        missing_previous = (
            replacement
            and self.db.execute(
                "SELECT 1 FROM revisions WHERE tenant=? AND id=?",
                (tenant, replacement["predecessor"]),
            ).fetchone()
            is None
        )
        if row is None or row["body_hash"] != action["expected_hash"] or missing_previous:
            # Never erase current policy or revive a predecessor when its decision
            # cannot be reconstructed from the restored content snapshot.
            self.db.execute(
                "UPDATE knowledge_state SET recovery_gap=1,generation=generation+1 WHERE tenant=?",
                (tenant,),
            )
            return
        position = self.s._event(tenant, "review", row["id"], at, action["actor"])
        self.db.execute(
            "INSERT INTO lifecycle VALUES (?,?,?,?,?)",
            (
                tenant,
                row["id"],
                position,
                action["decision"],
                json.dumps({"reason": action["reason"], "actor": action["actor"]}),
            ),
        )
        if replacement:
            self.db.execute(
                "INSERT INTO replacements VALUES (?,?,?,?,?,?,?)",
                (
                    tenant,
                    replacement["predecessor"],
                    row["id"],
                    position,
                    json.dumps(replacement["selector"]),
                    replacement["valid_start"],
                    replacement["valid_end"],
                ),
            )
        if action["decision"] == "accept":
            for other in self.db.execute(
                "SELECT * FROM revisions WHERE tenant=? AND family=? AND id!=?",
                (tenant, row["family"], row["id"]),
            ).fetchall():
                if self._state(tenant, other["id"], position)[0] == "accepted":
                    self.db.execute(
                        insert_ignore("conflicts", 4),
                        (tenant, other["id"], row["id"], position),
                    )
        self.db.execute(
            "UPDATE families SET version=version+1 WHERE tenant=? AND id=?", (tenant, row["family"])
        )
        self.db.execute(
            "UPDATE knowledge_state SET generation=generation+1 WHERE tenant=?", (tenant,)
        )
        self.s._enqueue(tenant, position, None, action["actor"])

    def purge_dependencies(self, tenant: str, observation: str) -> None:
        ids = [
            r[0]
            for r in self.db.execute(
                "SELECT revision FROM evidence_spans WHERE tenant=? AND observation=?",
                (tenant, observation),
            )
        ]
        for revision in ids:
            self.db.execute(
                "UPDATE revisions SET body=NULL,purged=1 WHERE tenant=? AND id=?",
                (tenant, revision),
            )
            self.db.execute(
                "UPDATE attestations SET statement=NULL WHERE tenant=? AND revision=?",
                (tenant, revision),
            )
        self.db.execute(
            "UPDATE knowledge_state SET generation=generation+1 WHERE tenant=?", (tenant,)
        )

    def attest(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        row = self._revision(who["tenant"], args["revision"], args["project"])
        if not self._available(who, row, at) or args["expires_ms"] <= at:
            raise LocalError("evidence_unavailable")
        position = self.s._event(who["tenant"], "attest", row["id"], at, who["principal"])
        ident = uid("att")
        self.db.execute(
            "INSERT INTO attestations VALUES (?,?,?,?,?,?,?)",
            (
                who["tenant"],
                ident,
                row["id"],
                who["principal"],
                position,
                args["expires_ms"],
                args["statement"],
            ),
        )
        self.s._enqueue(who["tenant"], position, None, who["principal"])
        return {"attestation": ident, "position": position}

    def promote(
        self, credential: str, who: dict[str, Any], args: dict[str, Any], at: int
    ) -> dict[str, Any]:
        source = self._revision(who["tenant"], args["revision"])
        self.s._auth(credential, "review", source["project"], at)
        self.s._auth(credential, "export", source["project"], at)
        if not self._available(who, source, at):
            raise LocalError("evidence_unavailable")
        if source["kind"] == "procedure":
            raise LocalError("procedure_review_required")
        body = json.loads(source["body"])
        body.pop("executable")
        body["provenance"] = {"promoted_from": source["id"], "source_hash": source["body_hash"]}
        # Original evidence dependencies remain; promotion cannot silently widen their audience.
        return self.propose(
            credential,
            who,
            {
                "project": args["project"],
                "logical_key": args["logical_key"],
                "expected_version": args["expected_version"],
                "body": body,
            },
            at,
        )

    def _eligible(
        self,
        row: sqlite3.Row,
        body: dict[str, Any],
        binding: dict[str, Any],
        valid: int,
        recorded: int,
        who: dict[str, Any] | None = None,
        at: int | None = None,
    ) -> selectors.Truth:
        if not temporal_valid(valid, body["valid_start"], body["valid_end"]):
            return "false"
        from memoryking.adapters.repository_store import Repository

        repo = Repository(self.s)
        ancestry = (
            (
                lambda r, a, c: repo.ancestry(
                    who, row["project"], r, a, c, binding.get("snapshot_id")
                )
            )
            if who is not None
            else None
        )
        match = selectors.evaluate(body["selector"], binding, ancestry)
        if match != "false" and who is not None and at is not None:
            code = repo.eligibility(who, row, binding, at)
            if code == "false":
                return "false"
            if code == "unknown":
                match = "unknown"
        if match == "false":
            return match
        for edge in self.db.execute(
            "SELECT * FROM replacements WHERE tenant=? AND predecessor=? AND position<=?",
            (row["tenant"], row["id"], recorded),
        ):
            if temporal_valid(valid, edge["valid_start"], edge["valid_end"]):
                mask = selectors.evaluate(json.loads(edge["selector"]), binding, ancestry)
                if mask == "true":
                    return "false"
                if mask == "unknown":
                    match = "unknown"
        return match

    def query(
        self,
        who: dict[str, Any],
        args: dict[str, Any],
        at: int,
        *,
        constraints: bool,
        attestation_limit: int = 256,
    ) -> dict[str, Any]:
        tenant = who["tenant"]
        state = self.db.execute(
            "SELECT * FROM knowledge_state WHERE tenant=?", (tenant,)
        ).fetchone()
        generation = state["generation"]
        if state["recovery_gap"]:
            return {"status": "blocked", "reason": "knowledge_reconciliation_required", "items": []}
        points = selectors.bindings(args["bindings"])
        frontier = self.db.execute(
            "SELECT position FROM tenants WHERE tenant=?", (tenant,)
        ).fetchone()[0]
        recorded = args.get("recorded_position", frontier)
        if recorded > frontier:
            raise LocalError("invalid_recorded_position")
        # Mandatory current policy cannot be waived by a historical query.
        if constraints:
            recorded, valid = frontier, at
        else:
            valid = args.get("valid_ms", at)
        rows = self.db.execute(
            "SELECT r.*,f.project,f.version AS family_version FROM revisions r JOIN families f ON r.tenant=f.tenant AND r.family=f.id WHERE r.tenant=? AND f.project IN (?,?,?) AND r.created<=? AND r.kind=? ORDER BY r.created LIMIT ?",
            (
                tenant,
                args["project"],
                "_tenant" if constraints else args["project"],
                "_workspace" if constraints else args["project"],
                recorded,
                "constraint" if constraints else "knowledge",
                REGISTRY_LIMIT + 1,
            ),
        ).fetchall()
        if len(rows) > REGISTRY_LIMIT:
            return {
                "status": "blocked",
                "reason": "constraint_resolution_incomplete"
                if constraints
                else "knowledge_resolution_incomplete",
                "items": [],
            }
        items: list[dict[str, Any]] = []
        unknown, unavailable = False, False
        matches_by_binding: list[list[dict[str, Any]]] = [[] for _ in points]
        evaluation_work = 0
        for row in rows:
            lifecycle, quarantine_then = self._state(tenant, row["id"], recorded)
            if lifecycle != "accepted" and not (args.get("diagnostic", False) and not constraints):
                continue
            if row["body"] is None:
                # Purged essential policy retains a blocking tombstone; no content leaks.
                if lifecycle == "accepted":
                    unavailable = True
                continue
            body = json.loads(row["body"])
            if any(body[k] is not None and body[k] != args.get(k) for k in ("task", "run")):
                # Missing scope can hide required task constraints, so block if unknown.
                if constraints and any(
                    body[k] is not None and args.get(k) is None for k in ("task", "run")
                ):
                    unknown = True
                continue
            if not body["executable"]:
                if args.get("diagnostic", False) and self._available(who, row, at):
                    items.append(
                        {
                            "revision": row["id"],
                            "state": lifecycle,
                            "authority": "proposed_interpretation",
                            "body": body,
                            "selector_status": "unsupported",
                        }
                    )
                continue
            mask_selectors = [
                json.loads(e[0])
                for e in self.db.execute(
                    "SELECT selector FROM replacements WHERE tenant=? AND predecessor=? AND position<=?",
                    (tenant, row["id"], recorded),
                )
            ]
            evaluation_work += len(points) * sum(
                len(c["all_of"])
                for selector in [body["selector"], *mask_selectors]
                for c in selector["any_of"]
            )
            if evaluation_work > 65536:
                return {"status": "blocked", "reason": "selector_limit_exceeded", "items": []}
            matches = [self._eligible(row, body, b, valid, recorded, who, at) for b in points]
            if all(m == "false" for m in matches):
                continue
            if "unknown" in matches:
                unknown = True
                # Never emit a partially validated code claim as current guidance
                # over a declared multi-path/snapshot scope.
                if (
                    not args.get("diagnostic", False)
                    and self.db.execute(
                        "SELECT 1 FROM code_policies WHERE tenant=? AND revision=?",
                        (tenant, row["id"]),
                    ).fetchone()
                ):
                    continue
            if not self._available(who, row, at):
                unavailable = unavailable or constraints
                continue
            _, quarantined_now = self._state(tenant, row["id"], frontier)
            if (quarantined_now or quarantine_then) and not args.get("diagnostic", False):
                unavailable = True
                continue
            if "true" not in matches and not args.get("diagnostic", False):
                continue
            attestations = [
                dict(a)
                for a in self.db.execute(
                    "SELECT actor,position,expires,statement FROM attestations WHERE tenant=? AND revision=? AND position<=? AND expires>? ORDER BY position LIMIT ?",
                    (tenant, row["id"], recorded, at, attestation_limit),
                )
            ]
            item = {
                "revision": row["id"],
                "family": row["family"],
                "family_version": row["family_version"],
                "body_hash": row["body_hash"],
                "created_position": row["created"],
                "origin": row["origin"],
                "state": lifecycle,
                "quarantined": quarantined_now or quarantine_then,
                "authority": "mandatory_constraint"
                if constraints
                else (
                    "accepted_knowledge" if lifecycle == "accepted" else "proposed_interpretation"
                ),
                "body": body,
                "applicability": matches,
                "attestations": attestations,
            }
            from memoryking.adapters.repository_store import Repository

            item["code_validation"] = [
                proof
                for b, match in zip(points, matches, strict=True)
                if match == "true"
                and (proof := Repository(self.s).proof(who, row, b, at)) is not None
            ]
            items.append(item)
            if lifecycle == "accepted":
                for i, match in enumerate(matches):
                    if match == "true":
                        matches_by_binding[i].append(item)
        # A restricted essential dependency must not disclose any hidden IDs/counts.
        if constraints and unavailable:
            return {"status": "blocked", "reason": "essential_policy_unavailable", "items": []}
        conflict_pairs: set[tuple[str, str]] = set()
        for applicable in matches_by_binding:
            for i, left in enumerate(applicable):
                for right in applicable[i + 1 :]:
                    lb, rb = left["body"], right["body"]
                    conflict = left["family"] == right["family"] and lb["text"] != rb["text"]
                    if constraints and lb["rule_key"] == rb["rule_key"]:
                        conflict |= (
                            lb["effect"] != rb["effect"] or lb["rule_value"] != rb["rule_value"]
                        )
                    if conflict:
                        conflict_pairs.add(tuple(sorted((left["revision"], right["revision"]))))
        reason = (
            "applicability_unknown"
            if unknown
            else "guidance_unavailable"
            if unavailable
            else "unresolved_conflict"
            if conflict_pairs
            else None
        )
        return {
            "status": "blocked" if reason else "complete",
            "reason": reason,
            "items": items,
            "conflicts": [list(pair) for pair in sorted(conflict_pairs)],
            "constraint_generation": generation if constraints else None,
            "recorded_position": recorded,
            "valid_ms": valid,
            "binding_count": len(points),
            "authority": "complete_registry" if constraints else "knowledge_inspection",
        }
