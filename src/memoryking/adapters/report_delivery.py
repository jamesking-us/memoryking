"""Independent report-delivery intent, lease and acceptance/unknown fences.

No delivery identity obtains fresh retry rights from a content-history rotation.
The SMTP adapter only runs after a durable claim and current recipient release.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from memoryking.adapters.local_files import Ownership, private_dir
from memoryking.adapters.local_store import LocalStore, uid
from memoryking.adapters.report_store import Reports
from memoryking.contracts.canonical import canonical_bytes, canonical_hash
from memoryking.domain.local import LocalError
from memoryking.reporting.smtp import GmailDestination, Outcome, message, submit


@dataclass(frozen=True)
class MailConfiguration:
    destination: GmailDestination
    enabled: bool = False
    project: str | None = None


def configured(store: LocalStore, destination: str | None, project: str) -> MailConfiguration:
    value = getattr(store, "report_mail", None)
    if (
        not isinstance(value, MailConfiguration)
        or value.destination.id != destination
        or (value.project is not None and value.project != project)
    ):
        raise LocalError("report_delivery_unavailable")
    return value


class Deliveries:
    def __init__(self, store: LocalStore):
        self.s, self.db = store, store.db

    def sender_status(self, project: str) -> dict[str, Any]:
        config = getattr(self.s, "report_mail", None)
        if not isinstance(config, MailConfiguration) or config.project not in {None, project}:
            return {
                "configured": False,
                "enabled": False,
                "destination": None,
                "recipient_fingerprint": None,
            }
        return {
            "configured": True,
            "enabled": config.enabled,
            "destination": config.destination.id,
            "recipient_fingerprint": config.destination.fingerprint,
        }

    def import_legacy(
        self, who: dict[str, Any], args: dict[str, Any], at: int, *, key: str, request_hash: str
    ) -> dict[str, Any]:
        """Reserve an operator-verified accepted legacy identity; never publish/send.

        The trusted collector verifies original receipts/artifacts. A source hash
        is evidence binding, not proof of provider acceptance or a new content ACL.
        """
        prior = self.db.execute(
            "SELECT request_hash,result FROM report_control_retries WHERE tenant=? AND actor=? AND key=?",
            (who["tenant"], who["principal"], key),
        ).fetchone()
        if prior is not None:
            if prior["request_hash"] != request_hash:
                raise LocalError("idempotency_conflict")
            return dict(json.loads(prior["result"]))
        legacy, project = args["legacy"], args["project"]
        config = configured(self.s, legacy["destination"], project)
        if config.enabled:
            raise LocalError("report_import_requires_disabled_sender")
        if config.destination.fingerprint != legacy["recipient_fingerprint"]:
            raise LocalError("report_destination_changed")
        reports = Reports(self.s)
        policy_version, policy = reports._policy(who["tenant"], project, legacy["program"])
        if (
            policy["channel"] not in {"kindle", "both"}
            or policy["destination"] != legacy["destination"]
            or policy.get("recipient_fingerprint") != legacy["recipient_fingerprint"]
        ):
            raise LocalError("report_delivery_policy_changed")
        if legacy["accepted_ms"] > at or legacy["evidence"]["sha256"] != legacy["receipt_sha256"]:
            raise LocalError("report_legacy_evidence_mismatch")
        reports._dependency(who, project, legacy["evidence"], at)
        reports._dependency(
            {"tenant": who["tenant"], "principal": policy["recipient_principal"]},
            project,
            legacy["evidence"],
            at,
        )
        digest = canonical_hash(
            {
                "kind": "legacy_report_delivery",
                "store": self.s.store_id,
                "tenant": who["tenant"],
                "legacy_identity": legacy["legacy_identity"],
            }
        )
        semantic = {"project": project, "report": args["report"], "legacy": legacy}
        existing = self.db.execute(
            "SELECT body FROM report_deliveries WHERE tenant=? AND id=?", (who["tenant"], digest)
        ).fetchone()
        if existing is not None:
            body = json.loads(existing["body"])
            if body.get("legacy_import_hash") != canonical_hash(semantic):
                raise LocalError("report_delivery_identity_conflict")
        else:
            for table in ("report_revisions", "report_deliveries"):
                if self.db.execute(
                    f"SELECT 1 FROM {table} WHERE tenant=? AND project=? AND report=? AND revision=?",
                    (who["tenant"], project, args["report"], legacy["revision"]),
                ).fetchone():
                    raise LocalError("report_delivery_identity_conflict")
            body = {
                "version": 1,
                "id": digest,
                "store": self.s.store_id,
                "tenant": who["tenant"],
                "project": project,
                "report": args["report"],
                "revision": legacy["revision"],
                "program": legacy["program"],
                "phase": legacy["phase"],
                "source_sha256": legacy["source_sha256"],
                "artifact_sha256": legacy["artifact_sha256"],
                "channel": "kindle",
                "destination": legacy["destination"],
                "recipient_fingerprint": legacy["recipient_fingerprint"],
                "recipient_principal": policy["recipient_principal"],
                "policy_version": policy_version,
                "source_incarnation": self.s.incarnation,
                "created_ms": legacy["accepted_ms"],
                "state": "smtp_accepted",
                "attempts": 0,
                "next_attempt_ms": 0,
                "lease_token": None,
                "lease_until": None,
                "release": None,
                "outcome": {"stage": "verified_legacy_import", "code": None},
                "outcome_ms": legacy["accepted_ms"],
                "message_id": legacy["message_id"],
                "device_delivery": "unverified",
                "reconciliation": [],
                "legacy_import_hash": canonical_hash(semantic),
                "legacy_identity": legacy["legacy_identity"],
                "legacy_receipt_sha256": legacy["receipt_sha256"],
                "imported_ms": at,
                "imported_by": who["principal"],
                "import_evidence": legacy["evidence"],
            }
        result = {
            **self._public(body),
            "imported": True,
            "artifact_sha256": body["artifact_sha256"],
            "receipt_sha256": body["legacy_receipt_sha256"],
            "report": body["report"],
            "revision": body["revision"],
        }
        self._write(
            who,
            body,
            "import_legacy",
            at,
            retry={"key": key, "request_hash": request_hash, "result": result},
        )
        return result

    def apply(self, action: dict[str, Any]) -> None:
        body = action["delivery"]
        self.db.execute(
            "DELETE FROM report_deliveries WHERE tenant=? AND id=?", (action["tenant"], body["id"])
        )
        self.db.execute(
            "INSERT INTO report_deliveries VALUES (?,?,?,?,?,?,?)",
            (
                action["tenant"],
                body["id"],
                body["project"],
                body["report"],
                body["revision"],
                body["state"],
                canonical_bytes(body).decode(),
            ),
        )

        if "retry" in action:
            retry = action["retry"]
            self.db.execute(
                "INSERT OR IGNORE INTO report_control_retries VALUES (?,?,?,?,?)",
                (
                    action["tenant"],
                    action["actor"],
                    retry["key"],
                    retry["request_hash"],
                    canonical_bytes(retry["result"]).decode(),
                ),
            )

    def _write(
        self,
        who: dict[str, Any],
        body: dict[str, Any],
        operation: str,
        at: int,
        retry: dict[str, Any] | None = None,
    ) -> None:
        action = {
            "kind": "report_delivery",
            "tenant": who["tenant"],
            "actor": who["principal"],
            "operation": operation,
            "delivery": body,
        }
        if retry is not None:
            action["retry"] = retry
        record = self.s._journal(action)
        self.s._apply(record, at)

    def _job(self, tenant: str, delivery: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT state,body FROM report_deliveries WHERE tenant=? AND id=?", (tenant, delivery)
        ).fetchone()
        if row is None:
            raise LocalError("unavailable")
        body = json.loads(row["body"])
        # Purge is itself a durable control action; its projection cancels unsent work.
        body["state"] = row["state"]
        return dict(body)

    def enqueue(
        self, who: dict[str, Any], row: Any, policy: dict[str, Any], policy_version: int, at: int
    ) -> str:
        """Called only inside the canonical publish transaction; no network here."""
        config = configured(self.s, policy["destination"], row["project"])
        if policy.get("recipient_fingerprint") != config.destination.fingerprint:
            raise LocalError("report_destination_changed")
        file = self.db.execute(
            "SELECT blob FROM report_files WHERE tenant=? AND project=? AND report=? AND revision=? AND name='report.epub' AND kind='artifact'",
            (who["tenant"], row["project"], row["report"], row["revision"]),
        ).fetchone()
        if file is None:
            raise LocalError("report_artifact_unavailable")
        report = json.loads(row["body"])
        recipient = {"tenant": who["tenant"], "principal": policy["recipient_principal"]}
        Reports(self.s)._allowed(recipient, report, at)
        identity = {
            "version": 1,
            "store": self.s.store_id,
            "tenant": who["tenant"],
            "project": row["project"],
            "report": row["report"],
            "revision": row["revision"],
            "artifact_sha256": file["blob"],
            "channel": "kindle",
            "destination": policy["destination"],
            "recipient_fingerprint": config.destination.fingerprint,
            "policy_version": policy_version,
        }
        digest = canonical_hash(identity)
        prior = self.db.execute(
            "SELECT body FROM report_deliveries WHERE tenant=? AND project=? AND report=? AND revision=?",
            (who["tenant"], row["project"], row["report"], row["revision"]),
        ).fetchall()
        for prior_row in prior:
            previous = json.loads(prior_row["body"])
            if previous["destination"] == policy["destination"]:
                # A policy version or history rotation cannot mint another automatic
                # delivery of an existing revision. New content needs a new revision.
                if (
                    previous["artifact_sha256"] != file["blob"]
                    or previous["recipient_fingerprint"] != config.destination.fingerprint
                ):
                    raise LocalError("report_delivery_identity_conflict")
                return str(previous["id"])
        body = {
            **identity,
            "id": digest,
            "source_incarnation": self.s.incarnation,
            "program": report["program"],
            "phase": report["phase"],
            "source_sha256": report["source_sha256"],
            "recipient_principal": policy["recipient_principal"],
            "created_ms": at,
            "state": "queued",
            "attempts": 0,
            "next_attempt_ms": at,
            "lease_token": None,
            "lease_until": None,
            "release": None,
            "outcome": None,
            "device_delivery": "unverified",
            "message_id": f"<mk-{digest}@memoryking.invalid>",
            "reconciliation": [],
        }
        self._write(who, body, "intent", at)
        return digest

    def _authorized(
        self, who: dict[str, Any], body: dict[str, Any], at: int
    ) -> tuple[Any, MailConfiguration]:
        if body["source_incarnation"] != self.s.incarnation:
            raise LocalError("history_changed")
        config = configured(self.s, body["destination"], body["project"])
        if not config.enabled:
            raise LocalError("report_sender_disabled")
        if config.destination.fingerprint != body["recipient_fingerprint"]:
            raise LocalError("report_destination_changed")
        reports = Reports(self.s)
        row = self.db.execute(
            "SELECT * FROM report_revisions WHERE tenant=? AND project=? AND report=? AND revision=?",
            (who["tenant"], body["project"], body["report"], body["revision"]),
        ).fetchone()
        if row is None or row["state"] != "published" or row["body"] is None:
            raise LocalError("report_not_published")
        report = json.loads(row["body"])
        version, policy = reports._policy(who["tenant"], body["project"], body["program"])
        if (
            version != body["policy_version"]
            or policy["channel"] not in {"kindle", "both"}
            or policy["destination"] != body["destination"]
            or policy["recipient_principal"] != body["recipient_principal"]
            or policy.get("recipient_fingerprint") != body["recipient_fingerprint"]
        ):
            raise LocalError("report_delivery_policy_changed")
        reports._allowed(who, report, at)
        reports._allowed(
            {"tenant": who["tenant"], "principal": body["recipient_principal"]}, report, at
        )
        file = self.db.execute(
            "SELECT blob FROM report_files WHERE tenant=? AND project=? AND report=? AND revision=? AND name='report.epub' AND kind='artifact'",
            (who["tenant"], body["project"], body["report"], body["revision"]),
        ).fetchone()
        if (
            file is None
            or file["blob"] != body["artifact_sha256"]
            or report["source_sha256"] != body["source_sha256"]
        ):
            raise LocalError("report_artifact_changed")
        return row, config

    def attempt(
        self,
        credential: str,
        project: str,
        delivery: str,
        *,
        smtp_factory: Callable[..., Any] | None = None,
    ) -> dict[str, Any]:
        directory = self.s.control_path / "report-mail-worker"
        private_dir(directory, create=True)
        owner = Ownership(directory)
        try:
            return self._attempt(credential, project, delivery, smtp_factory=smtp_factory)
        finally:
            owner.close()

    def _attempt(
        self,
        credential: str,
        project: str,
        delivery: str,
        *,
        smtp_factory: Callable[..., Any] | None,
    ) -> dict[str, Any]:
        token = uid("mail")
        with self.s.transaction() as decision:
            at = decision.evaluated_ms
            who = self.s._auth(credential, "report_delivery", project, at)
            body = self._job(who["tenant"], delivery)
            if body["project"] != project:
                raise LocalError("unavailable")
            if body["state"] == "submitting":
                # The process lock is now ours, so the former worker is gone. A
                # claimed attempt is conservatively unknown even if it died early.
                body.update(
                    state="unknown", outcome={"stage": "recovery", "code": None}, lease_until=None
                )
                self._write(who, body, "recover_unknown", at)
            if body["state"] in {"smtp_accepted", "unknown", "rejected", "cancelled"}:
                result = self._public(body)
                raw = None
                config = None
            else:
                if body["state"] != "queued" or body["attempts"] >= 3:
                    raise LocalError("report_delivery_retry_limit")
                if at < body["next_attempt_ms"]:
                    raise LocalError("report_delivery_backoff")
                row, config = self._authorized(who, body, at)
                epub = Reports(self.s)._bytes(
                    who["tenant"], body["artifact_sha256"], 60 * 1024 * 1024
                )
                raw = message(
                    config.destination,
                    delivery=body["id"],
                    report=body["report"],
                    revision=body["revision"],
                    title=json.loads(row["body"])["title"],
                    epub=epub,
                    created_ms=body["created_ms"],
                )
                body.update(
                    state="submitting",
                    attempts=body["attempts"] + 1,
                    lease_token=token,
                    lease_until=at + 120000,
                    release=None,
                    outcome=None,
                    mime_sha256=hashlib.sha256(raw).hexdigest(),
                )
                self._write(who, body, "claim", at)
                result = self._public(body)
        self.s._expose()
        if raw is None or config is None:
            decision.check(self.s.clock)
            return result
        self.s.fault("report_mail_after_claim")

        def authorize() -> Callable[[], None]:
            with self.s.transaction() as release:
                actor = self.s._auth(credential, "report_delivery", project, release.evaluated_ms)
                current = self._job(actor["tenant"], delivery)
                if (
                    current["state"] != "submitting"
                    or current["lease_token"] != token
                    or current["lease_until"] <= release.evaluated_ms
                ):
                    raise LocalError("report_delivery_fenced")
                self._authorized(actor, current, release.evaluated_ms)
                current["release"] = {
                    "evaluated_ms": release.evaluated_ms,
                    "deadline_ms": release.deadline_ms,
                }
                self._write(actor, current, "authorize_data", release.evaluated_ms)
            self.s._expose()
            return lambda: release.check(self.s.clock)

        try:
            options = {} if smtp_factory is None else {"smtp_factory": smtp_factory}
            outcome = submit(config.destination, raw, authorize, **options)
        except LocalError as error:
            # The adapter only raises LocalError before any content byte is sent.
            outcome = Outcome(
                "pre_data_transient" if error.code == "release_expired" else "rejected", error.code
            )
        self.s.fault("report_mail_after_provider")
        with self.s.transaction() as completion:
            # Receipt persistence must not erase observed provider acceptance when
            # an actor grant changes after the ordered release instant.
            current = self._job(who["tenant"], delivery)
            if current["lease_token"] != token or current["state"] != "submitting":
                raise LocalError("report_delivery_fenced")
            state: str = outcome.state
            if state == "pre_data_transient":
                state = "queued" if current["attempts"] < 3 else "rejected"
            current.update(
                state=state,
                outcome={"stage": outcome.stage, "code": outcome.code},
                outcome_ms=completion.evaluated_ms,
                lease_token=None,
                lease_until=None,
                next_attempt_ms=completion.evaluated_ms
                + (1000 if current["attempts"] == 1 else 5000),
            )
            self._write(who, current, "outcome", completion.evaluated_ms)
            result = self._public(current)
        self.s._expose()
        completion.check(self.s.clock)
        return result

    @staticmethod
    def _public(body: dict[str, Any]) -> dict[str, Any]:
        return {
            "delivery": body["id"],
            "state": body["state"],
            "attempts": body["attempts"],
            "device_delivery": "unverified",
            "message_id": body["message_id"],
            "outcome_ms": body.get("outcome_ms"),
            "outcome_source": (body.get("outcome") or {}).get("stage"),
        }

    def reconcile(
        self, who: dict[str, Any], args: dict[str, Any], at: int, *, key: str, request_hash: str
    ) -> dict[str, Any]:
        """Explicit operator evidence; never infer an outcome from a Message-ID."""
        directory = self.s.control_path / "report-mail-worker"
        private_dir(directory, create=True)
        owner = Ownership(directory)
        try:
            body = self._job(who["tenant"], args["delivery"])
            if body["project"] != args["project"]:
                raise LocalError("unavailable")
            prior = self.db.execute(
                "SELECT request_hash,result FROM report_control_retries WHERE tenant=? AND actor=? AND key=?",
                (who["tenant"], who["principal"], key),
            ).fetchone()
            if prior is not None:
                if prior["request_hash"] != request_hash:
                    raise LocalError("idempotency_conflict")
                return dict(json.loads(prior["result"]))
            if (
                body["state"] not in {"submitting", "unknown"}
                or body["attempts"] != args["expected_attempts"]
            ):
                raise LocalError("report_reconciliation_conflict")
            Reports(self.s)._dependency(who, args["project"], args["evidence"], at)
            if args["outcome"] == "not_submitted":
                # A current recipient/history check is required even when an
                # operator has evidence that the former attempt sent nothing.
                self._authorized(who, body, at)
                body.update(state="queued", attempts=0, next_attempt_ms=at)
            else:
                body["state"] = "smtp_accepted"
                body["outcome_ms"] = at
            if len(body["reconciliation"]) >= 32:
                raise LocalError("report_reconciliation_limit")
            body["reconciliation"].append(
                {
                    "actor": who["principal"],
                    "evaluated_ms": at,
                    "evidence": args["evidence"],
                    "outcome": args["outcome"],
                }
            )
            body.update(
                lease_token=None,
                lease_until=None,
                outcome={"stage": "operator_reconciliation", "code": None},
            )
            result = self._public(body)
            self._write(
                who,
                body,
                "reconcile",
                at,
                {"key": key, "request_hash": request_hash, "result": result},
            )
            return result
        finally:
            owner.close()
