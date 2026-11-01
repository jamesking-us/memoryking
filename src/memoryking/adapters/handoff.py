"""Recipient-bound full handoffs. Preparation never sends content or transfers ownership."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator

from memoryking.adapters.local_store import RETRY_MS, uid
from memoryking.adapters.token_count import counter
from memoryking.adapters.working import Working
from memoryking.application.store_core import StoreCore
from memoryking.contracts.canonical import canonical_bytes, canonical_hash
from memoryking.domain.briefing import counted
from memoryking.domain.checkpoint import stream_name
from memoryking.domain.local import Decision, LocalError, Released
from memoryking.domain.selectors import bindings

VALIDATOR = Draft202012Validator(
    json.loads(Path(__file__).with_name("handoff.schema.json").read_text())
)


class Handoff:
    def __init__(self, store: StoreCore):
        self.s, self.db = store, store.db
        self.w = Working(store)
        self.r = self.w.r

    def _identity(
        self, tenant: str, principal: str, project: str, at: int, capability: str = "read"
    ) -> dict[str, Any]:
        return self.s._subject_auth(
            self.s._token("credential", tenant=tenant, principal=principal), capability, project, at
        )

    @staticmethod
    def _scope(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "project": row["project"],
            "task": row["task"],
            "run": None if row["stream"] == "task" else row["stream"][4:],
            "checkpoint": row["checkpoint"],
            "bindings": json.loads(row["bindings"]),
        }

    def _row(self, who: dict[str, Any], project: str, identifier: str, action: str) -> sqlite3.Row:
        role = "recipient" if action in {"materialize", "ack"} else "sender"
        row = self.db.execute(
            "SELECT * FROM handoffs WHERE tenant=? AND project=? AND id=?",
            (who["tenant"], project, identifier),
        ).fetchone()
        if row is None or row[role] != who["principal"]:
            raise LocalError("handoff_unavailable")
        if row["incarnation"] != self.s.incarnation:
            raise LocalError("history_changed")
        return cast(sqlite3.Row, row)

    def _active(self, row: sqlite3.Row, at: int) -> tuple[dict[str, Any], dict[str, Any]]:
        if row["state"] not in {"prepared", "acknowledged"}:
            raise LocalError("handoff_unavailable")
        if at >= row["expires"]:
            raise LocalError("handoff_expired")
        try:
            sender = self._identity(row["tenant"], row["sender"], row["project"], at, "handoff")
            self._identity(row["tenant"], row["sender"], row["project"], at)
            recipient = self._identity(row["tenant"], row["recipient"], row["project"], at)
            args = self._scope(row)
            self.w._checkpoint(sender, args, at)
            self.w._checkpoint(recipient, args, at)
            stream = self.w._stream(sender, args)
            if (
                stream is None
                or stream["latest"] != row["checkpoint"]
                or stream["revision"] != row["stream_revision"]
                or stream["incarnation"] != self.s.incarnation
            ):
                raise LocalError("handoff_superseded")
        except LocalError as exc:
            if exc.code == "handoff_superseded":
                raise
            raise LocalError("handoff_unavailable") from exc
        return sender, recipient

    def _prepare_check(self, who: dict[str, Any], args: dict[str, Any], at: int) -> sqlite3.Row:
        bindings(args["bindings"])
        try:
            recipient = self._identity(who["tenant"], args["recipient"], args["project"], at)
            self.w._checkpoint(who, args, at)
            self.w._checkpoint(recipient, args, at)
            stream = self.w._stream(who, args)
            if (
                stream is None
                or stream["latest"] != args["checkpoint"]
                or stream["incarnation"] != self.s.incarnation
            ):
                raise LocalError("handoff_superseded")
        except LocalError as exc:
            if exc.code == "handoff_superseded":
                raise
            raise LocalError("handoff_unavailable") from exc
        return stream

    def _snapshot(
        self, row: sqlite3.Row, recipient: dict[str, Any], at: int
    ) -> tuple[str, dict[str, Any], list[dict[str, str]]]:
        args = self._scope(row)
        policy = self.r.k.query(
            recipient,
            {k: args[k] for k in ("project", "task", "run", "bindings") if args[k] is not None},
            at,
            constraints=True,
        )
        # Do not bind to the wall clock or unrelated receipt/event positions.
        policy = {k: v for k, v in policy.items() if k not in {"valid_ms", "recorded_position"}}
        operations, deps = self.w._operations(recipient, args, at, include_all=True)
        deps.extend(self.r._dependencies([self.r._claim(item) for item in policy["items"]]))
        checkpoint = self.w._checkpoint(recipient, args, at)
        available = []
        for artifact in json.loads(checkpoint["body"])["repository"]["artifacts"]:
            if artifact["availability"] == "stored":
                source = self.db.execute(
                    "SELECT * FROM observations WHERE tenant=? AND id=?",
                    (row["tenant"], artifact["observation"]),
                ).fetchone()
                try:
                    self.r._blob(source)
                    available.append(True)
                except (LocalError, OSError):
                    available.append(False)
        epoch = self.db.execute(
            "SELECT policy_epoch,purge_epoch FROM tenants WHERE tenant=?", (row["tenant"],)
        ).fetchone()
        stamp = canonical_hash(
            {
                "compiler_revision": "handoff-v1",
                "policy": policy,
                "operations": operations,
                "checkpoint_hash": checkpoint["body_hash"],
                "stream_revision": row["stream_revision"],
                "epochs": list(epoch),
                "stored_artifacts_available": available,
            }
        )
        return stamp, policy, deps

    def _intersection(
        self,
        row: sqlite3.Row,
        sender: dict[str, Any],
        policy: dict[str, Any],
        deps: list[dict[str, str]],
        at: int,
    ) -> bool:
        if policy["status"] != "complete":
            return False
        try:
            self.w._operations(sender, self._scope(row), at, include_all=True)
            for dep in deps:
                self.w._dependency(sender, dep, at)
        except LocalError:
            return False
        return True

    def verify_dependency(self, who: dict[str, Any], identifier: str, at: int) -> None:
        view = self.db.execute(
            "SELECT * FROM handoff_views WHERE tenant=? AND id=? AND actor=?",
            (who["tenant"], identifier, who["principal"]),
        ).fetchone()
        if view is None:
            raise LocalError("handoff_unavailable")
        target = self.db.execute(
            "SELECT project FROM handoffs WHERE tenant=? AND id=?",
            (who["tenant"], view["handoff"]),
        ).fetchone()
        if target is None:
            raise LocalError("handoff_unavailable")
        row = self._row(
            who,
            target["project"],
            view["handoff"],
            view["mode"],
        )
        sender, recipient = self._active(row, at)
        stamp, policy, deps = self._snapshot(row, recipient, at)
        if view["stamp"] != stamp:
            raise LocalError("handoff_view_stale")
        if (
            view["mode"] == "preview"
            and view["status"] != "blocked"
            and not self._intersection(row, sender, policy, deps, at)
        ):
            raise LocalError("handoff_view_stale")

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
            who = self.s._auth(credential, "read", args["project"], at)
            if action in {"prepare", "preview", "cancel"}:
                self.s._auth(credential, "handoff", args["project"], at)
            self.r._expire(at)
            row = None
            if action == "prepare":
                stream = self._prepare_check(who, args, at)
            else:
                row = self._row(who, args["project"], args["handle"], action)
                if action != "cancel":
                    self._active(row, at)
            lookup = (
                who["tenant"],
                self.s.incarnation,
                "handoff-v1",
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
                result = json.loads(old["result"])
                if action in {"materialize", "preview"}:
                    artifact = self.r._artifact(who, args["project"], result["materialization"], at)
                    raw = bytes(artifact["body"])
                    deps = json.loads(artifact["dependencies"])
                elif action == "prepare":
                    self._active(self._row(who, args["project"], result["handle"], "prepare"), at)
                elif action == "ack":
                    self._ack_check(who, cast(sqlite3.Row, row), args, at)
            elif action == "prepare":
                result = self._prepare(who, args, stream, at)
            elif action in {"materialize", "preview"}:
                raw, deps = self._render(
                    who, cast(sqlite3.Row, row), action, args, decision, receipt
                )
                result = {"materialization": receipt}
            elif action == "ack":
                self._ack_check(who, cast(sqlite3.Row, row), args, at)
                self.db.execute(
                    "UPDATE handoffs SET state='acknowledged',ack_view=? WHERE tenant=? AND id=?",
                    (args["materialization"], who["tenant"], args["handle"]),
                )
                self._event(who, "handoff_acknowledged", args["handle"], at)
                result = {
                    "handle": args["handle"],
                    "status": "acknowledged",
                    "materialization": args["materialization"],
                    "task_complete": False,
                }
            else:
                self.db.execute(
                    "UPDATE handoffs SET state='cancelled' WHERE tenant=? AND id=?",
                    (who["tenant"], args["handle"]),
                )
                self._event(who, "handoff_cancelled", args["handle"], at)
                result = {"handle": args["handle"], "status": "cancelled"}
            if old is None:
                self.db.execute(
                    "INSERT INTO retries VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (*lookup, canonical_hash(command), json.dumps(result), None, at + RETRY_MS),
                )
            if action not in {"materialize", "preview"}:
                result.update(
                    receipt=receipt,
                    incarnation=self.s.incarnation,
                    evaluated_ms=at,
                    deadline_ms=decision.deadline_ms,
                    replayed=old is not None,
                )
                raw = canonical_bytes(result)
            epoch = self.db.execute(
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
                    *epoch,
                    hashlib.sha256(raw).hexdigest(),
                    json.dumps([d["id"] for d in deps if d["kind"] == "observation"]),
                ),
            )
            self.s.fault("after_validation")
        self.s._expose()
        return Released(raw, decision, self.s.clock, receipt)

    def _event(self, who: dict[str, Any], kind: str, identifier: str, at: int) -> None:
        position = self.s._event(who["tenant"], kind, identifier, at, who["principal"])
        self.s._enqueue(who["tenant"], position, None, who["principal"])

    def _prepare(
        self, who: dict[str, Any], args: dict[str, Any], stream: sqlite3.Row, at: int
    ) -> dict[str, Any]:
        if (
            self.db.execute(
                "SELECT count(*) FROM handoffs WHERE tenant=?", (who["tenant"],)
            ).fetchone()[0]
            >= 4096
        ):
            raise LocalError("handoff_capacity")
        identifier = uid("hnd")
        scope = (
            who["tenant"],
            args["project"],
            args["task"],
            stream_name(args["run"]),
            who["principal"],
            args["recipient"],
        )
        self.db.execute(
            "UPDATE handoffs SET state='superseded' WHERE tenant=? AND project=? AND task=? AND stream=? AND sender=? AND recipient=? AND state IN ('prepared','acknowledged')",
            scope,
        )
        self.db.execute(
            "INSERT INTO handoffs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                who["tenant"],
                identifier,
                args["project"],
                args["task"],
                stream_name(args["run"]),
                args["checkpoint"],
                who["principal"],
                args["recipient"],
                args["role"],
                args["stage"],
                json.dumps(args["bindings"]),
                self.s.incarnation,
                at,
                at + args["ttl_ms"],
                "prepared",
                stream["revision"],
                None,
            ),
        )
        self._event(who, "handoff_prepared", identifier, at)
        # A hand-off carries the sender's "where this left off" so the recipient reads
        # three sentences before the full packet; the checkpoint remains the record.
        from memoryking.adapters.working import Working

        summary_receipt = uid("rcpt")
        summary = Working(self.s)._summarize(
            who,
            {
                "project": args["project"],
                "task": args["task"],
                "run": args["run"],
                "checkpoint": args["checkpoint"],
                "source": "extractive",
                "text": None,
                "permit": None,
                "readers": None,
            },
            at,
            summary_receipt,
        )
        return {
            "handle": identifier,
            "status": "prepared",
            "expires_ms": at + args["ttl_ms"],
            "summary": {
                "receipt": summary_receipt,
                "text": summary["summary"],
                "method": summary["method"],
            },
        }

    def _render(
        self,
        who: dict[str, Any],
        row: sqlite3.Row,
        mode: str,
        args: dict[str, Any],
        decision: Decision,
        receipt: str,
    ) -> tuple[bytes, list[dict[str, str]]]:
        at = decision.evaluated_ms
        deps: list[dict[str, str]]
        sender, recipient = self._active(row, at)
        stamp, policy, view_deps = self._snapshot(row, recipient, at)
        resume = {
            **self._scope(row),
            "max_output_tokens": args["max_output_tokens"],
            "tokenizer": args["tokenizer"],
        }
        meta = {
            "compiler_revision": "handoff-v1",
            "handle": row["id"],
            "sender": row["sender"],
            "recipient": row["recipient"],
            "role": row["role"],
            "stage": row["stage"],
            "mode": mode,
            "materialization": receipt,
            "expires_ms": row["expires"],
        }
        if mode == "preview" and not self._intersection(row, sender, policy, view_deps, at):
            count, usage = counter(args["tokenizer"])
            base = self.r._base(args, decision, receipt, usage)
            base.update(
                status="blocked",
                reason="handoff_preview_incomplete",
                checkpoint=None,
                operations=[],
                handoff=meta,
            )
            raw, used = counted(base, count)
            if used > args["max_output_tokens"]:
                base["usage"]["budget_compliant"] = False
                raw, used = counted(base, count)
            deps = []
        else:
            extras: dict[str, Any] = {"handoff": meta}
            if mode == "preview":
                extras["context_binding"] = {
                    "store": self.s.store_id,
                    "tenant": who["tenant"],
                    "principal": who["principal"],
                    "incarnation": self.s.incarnation,
                    "policy_epoch": self.db.execute(
                        "SELECT policy_epoch FROM tenants WHERE tenant=?", (who["tenant"],)
                    ).fetchone()[0],
                }
            raw, deps = self.w._resume(
                recipient, resume, decision, receipt, extras=extras, retain=False
            )
        body = json.loads(raw)
        if (
            self.db.execute(
                "SELECT count(*) FROM handoff_views WHERE tenant=?", (who["tenant"],)
            ).fetchone()[0]
            >= 4096
        ):
            raise LocalError("handoff_view_capacity")
        self.db.execute(
            "INSERT INTO handoff_views VALUES (?,?,?,?,?,?,?,?,?)",
            (
                who["tenant"],
                receipt,
                row["id"],
                who["principal"],
                mode,
                stamp,
                hashlib.sha256(raw).hexdigest(),
                body["status"],
                at,
            ),
        )
        deps.append({"kind": "handoff_view", "id": receipt})
        diag = {
            "kind": "handoff_" + mode,
            "status": body["status"],
            "usage": body["usage"],
            "trace": [],
        }
        self.r._retain(who, args, receipt, raw, diag, deps, at)
        self._event(who, "handoff_" + mode, row["id"], at)
        return raw, deps

    def _ack_check(
        self, who: dict[str, Any], row: sqlite3.Row, args: dict[str, Any], at: int
    ) -> None:
        view = self.db.execute(
            "SELECT * FROM handoff_views WHERE tenant=? AND id=? AND handoff=? AND actor=? AND mode='materialize'",
            (who["tenant"], args["materialization"], row["id"], who["principal"]),
        ).fetchone()
        if (
            view is None
            or view["body_hash"] != args["rendered_hash"]
            or view["status"] not in {"ready", "incomplete"}
        ):
            raise LocalError("handoff_ack_invalid")
        self.r._artifact(who, args["project"], view["id"], at)
