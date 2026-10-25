"""Immutable, audience-bound outcome/feedback revisions; observed retrieval is not usefulness."""

from __future__ import annotations

import json
from typing import Any

from memoryking.adapters.knowledge_store import Knowledge
from memoryking.adapters.local_store import LocalStore, uid
from memoryking.adapters.retrieval import Retrieval
from memoryking.contracts.canonical import canonical_bytes, canonical_hash
from memoryking.domain.local import LocalError


class ExperienceRecords:
    def __init__(self, store: LocalStore):
        self.s, self.db = store, store.db

    def available(self, who: dict[str, Any], row: Any, at: int) -> bool:
        if row["body"] is None or row["incarnation"] != self.s.incarnation:
            return False
        if canonical_hash(json.loads(row["body"])) != row["body_hash"]:
            return False
        readers = json.loads(row["readers"])
        if readers is not None and who["principal"] not in readers:
            return False
        try:
            self.s._subject_auth(
                self.s._token("credential", tenant=who["tenant"], principal=who["principal"]),
                "read",
                row["project"],
                at,
            )
            for dep in json.loads(row["dependencies"]):
                Retrieval(self.s)._dependency(who, dep, at)
        except LocalError:
            return False
        return True

    def get(self, who: dict[str, Any], project: str, reference: str, kind: str, at: int) -> Any:
        row = self.db.execute(
            "SELECT * FROM experience_records WHERE tenant=? AND project=? AND id=? AND kind=?",
            (who["tenant"], project, reference, kind),
        ).fetchone()
        if row is None or not self.available(who, row, at):
            raise LocalError("experience_record_unavailable")
        if canonical_hash(json.loads(row["body"])) != row["body_hash"]:
            raise LocalError("experience_integrity_failure")
        return row

    def save(
        self,
        who: dict[str, Any],
        project: str,
        kind: str,
        key: str,
        expected: int,
        body: dict[str, Any],
        deps: list[dict[str, str]],
        readers: list[str] | None,
        at: int,
    ) -> dict[str, Any]:
        if readers is not None and who["principal"] not in readers:
            raise LocalError("experience_actor_audience_required")
        if len(canonical_bytes(body)) > 65536 or len(deps) > 256:
            raise LocalError("experience_record_limit")
        current = (
            self.db.execute(
                "SELECT max(revision) FROM experience_records WHERE tenant=? AND project=? AND principal=? AND incarnation=? AND kind=? AND logical_key=?",
                (who["tenant"], project, who["principal"], self.s.incarnation, kind, key),
            ).fetchone()[0]
            or 0
        )
        if current != expected:
            raise LocalError("experience_revision_conflict")
        if (
            self.db.execute(
                "SELECT count(*) FROM experience_records WHERE tenant=?", (who["tenant"],)
            ).fetchone()[0]
            >= 10000
        ):
            raise LocalError("experience_capacity")
        for dep in deps:
            Retrieval(self.s)._dependency(who, dep, at)
        ident = uid("exp")
        position = self.s._event(who["tenant"], "experience_" + kind, ident, at, who["principal"])
        self.db.execute(
            "INSERT INTO experience_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                who["tenant"],
                ident,
                project,
                who["principal"],
                self.s.incarnation,
                kind,
                key,
                current + 1,
                json.dumps(body),
                canonical_hash(body),
                json.dumps(deps),
                json.dumps(readers),
                position,
            ),
        )
        self.s._enqueue(who["tenant"], position, None, who["principal"])
        return {
            "record": ident,
            "revision": current + 1,
            "kind": kind,
            "position": position,
            "assurance": "actor_reported",
            "body_hash": canonical_hash(body),
        }

    def outcome(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        body = args["body"]
        Knowledge(self.s)._support(who, body["acceptance_evidence"], at)
        if body["result"] == "success" and not body["acceptance_evidence"]:
            raise LocalError("acceptance_evidence_required")
        if body["cached_input_tokens"] is not None and (
            body["input_tokens"] is None or body["cached_input_tokens"] > body["input_tokens"]
        ):
            raise LocalError("invalid_usage")
        deps = [
            {"kind": "observation", "id": source}
            for source in sorted({e["observation"] for e in body["acceptance_evidence"]})
        ]
        return self.save(
            who,
            args["project"],
            "outcome",
            args["logical_key"],
            args["expected_revision"],
            {
                **body,
                "assurance": "actor_reported",
                "acceptance_semantics": "evidence_bytes_checked_result_not_independently_judged",
                "usage_assurance": "actor_reported_unknowns_preserved",
            },
            deps,
            body["readers"],
            at,
        )

    def feedback(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        r = Retrieval(self.s)
        artifact = r._artifact(who, args["project"], args["receipt"], at)
        packet = json.loads(bytes(artifact["body"]))
        request = json.loads(artifact["request_json"])
        if request.get("task") not in {None, args["task"]} or request.get("run") not in {
            None,
            args["run"],
        }:
            raise LocalError("feedback_scope_mismatch")
        item = next(
            (
                i
                for i in packet.get("guidance", []) + packet.get("constraints", [])
                if i["reference"] == args["reference"]
            ),
            None,
        )
        if item is None:
            raise LocalError("feedback_reference_not_retrieved")
        if args["usefulness"] == "helpful" and args["use"] != "used":
            raise LocalError("feedback_use_required")
        deps = r._dependencies([item])
        body = {
            key: value
            for key, value in args.items()
            if key not in {"project", "expected_revision", "readers"}
        }
        body.update(
            retrieved="receipt_verified",
            use_assurance="actor_reported",
            usefulness_assurance="actor_reported",
            receipt_hash=artifact["body_hash"],
            target_hash=canonical_hash(item),
            causal_claim=False,
        )
        # A new receipt/exposure cannot create another independent vote for the same run/item.
        key = canonical_hash([args["task"], args["run"], args["reference"]])
        return self.save(
            who,
            args["project"],
            "feedback",
            key,
            args["expected_revision"],
            body,
            deps,
            args["readers"],
            at,
        )

    def records(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        rows = self.db.execute(
            "SELECT e.* FROM experience_records e WHERE e.tenant=? AND e.project=? AND e.incarnation=? AND e.kind=? AND (e.readers='null' OR EXISTS(SELECT 1 FROM json_each(e.readers) WHERE value=?)) AND NOT EXISTS(SELECT 1 FROM experience_records n WHERE n.tenant=e.tenant AND n.project=e.project AND n.principal=e.principal AND n.incarnation=e.incarnation AND n.kind=e.kind AND n.logical_key=e.logical_key AND n.revision>e.revision) ORDER BY e.position DESC LIMIT 257",
            (who["tenant"], args["project"], self.s.incarnation, args["kind"], who["principal"]),
        ).fetchall()
        items = []
        for row in rows[:256]:
            if self.available(who, row, at):
                items.append(
                    {
                        "record": row["id"],
                        "revision": row["revision"],
                        "kind": row["kind"],
                        "actor": row["principal"],
                        "position": row["position"],
                        "body": json.loads(row["body"]),
                        "body_hash": row["body_hash"],
                    }
                )
        return {
            "status": "partial" if len(rows) > 256 else "complete",
            "items": items,
            "coverage": "current_authorized_latest_revisions",
            "limit": 256,
        }

    def record(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        row = self.get(who, args["project"], args["record"], args["kind"], at)
        return {
            "record": row["id"],
            "kind": row["kind"],
            "revision": row["revision"],
            "actor": row["principal"],
            "body": json.loads(row["body"]),
            "body_hash": row["body_hash"],
            "position": row["position"],
        }

    def verify_dependency(self, who: dict[str, Any], reference: str, at: int) -> None:
        row = self.db.execute(
            "SELECT * FROM experience_records WHERE tenant=? AND id=?", (who["tenant"], reference)
        ).fetchone()
        if row is None or not self.available(who, row, at):
            raise LocalError("replay_denied")
