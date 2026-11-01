"""Exact-request authorization for the trusted, local-only reference dispatcher."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from memoryking.adapters.egress_policy import (
    LOCAL_MODEL,
    LOCAL_PROVIDER,
    LOCAL_REVISION,
    ProviderPolicy,
    rank,
)
from memoryking.adapters.local_store import RETRY_MS, LocalStore, identifier, uid
from memoryking.adapters.retrieval import Retrieval
from memoryking.contracts.canonical import canonical_bytes, canonical_hash
from memoryking.domain.local import Decision, LocalError, Released

VALIDATOR = Draft202012Validator(
    json.loads(Path(__file__).with_name("inference.schema.json").read_text())
)
EGRESS_REVISION = LOCAL_REVISION
AUDIT_PAGE = 64


class Inference:
    """A credential with inference capability attests trusted host provenance.

    The server verifies byte coverage and every referenced MemoryKing artifact. It
    cannot infer whether an arbitrary host lied about a derived segment's origins.
    The ordinary model-facing tools deliberately cannot invoke this surface.
    """

    def __init__(self, store: LocalStore):
        self.s, self.db = store, store.db
        self.r = Retrieval(store)

    def _binding(self, credential: str, binding: dict[str, Any], at: int) -> dict[str, Any]:
        """Authorize the caller and check the binding against the store's current state."""
        project = binding["project"]
        who = self.s._auth(credential, "inference", project, at)
        self.s._auth(credential, "read", project, at)
        epochs = self.db.execute(
            "SELECT policy_epoch,purge_epoch FROM tenants WHERE tenant=?", (who["tenant"],)
        ).fetchone()
        expected = {
            "store": self.s.store_id,
            "tenant": who["tenant"],
            "principal": who["principal"],
            "project": project,
            "incarnation": self.s.incarnation,
            "authorization_epoch": epochs[0],
            "purge_epoch": epochs[1],
            "delegation_chain": who.get("delegation_chain", []),
            "egress_policy_revision": self.s.egress_revision(),
        }
        if any(binding[key] != value for key, value in expected.items()):
            raise LocalError("inference_context_changed")
        return who

    def _provider(self, request: dict[str, Any]) -> ProviderPolicy | None:
        """The policy entry a request may leave through; None for the local recorder."""
        if request["provider"] == LOCAL_PROVIDER:
            if request["model"] != LOCAL_MODEL:
                raise LocalError("egress_provider_denied")
            return None
        policy = self.s.egress_policy
        entry: ProviderPolicy | None = (
            policy.provider(request["provider"]) if policy is not None else None
        )
        if entry is None or not entry.allows(request["model"]):
            raise LocalError("egress_provider_denied")
        if len(canonical_bytes(request)) > entry.max_request_bytes:
            raise LocalError("inference_request_limit")
        return entry

    def _classification(self, tenant: str, dependencies: list[dict[str, str]]) -> str:
        """The highest classification among the observations a receipt depended on.

        A claim is traced to its evidence observations; working-state and procedure
        dependencies carry no classification of their own and count as internal.
        """
        highest = "public"
        for dep in dependencies:
            if dep["kind"] == "observation":
                rows = self.db.execute(
                    "SELECT classification FROM observations WHERE tenant=? AND id=?",
                    (tenant, dep["id"]),
                ).fetchall()
            elif dep["kind"] == "revision":
                rows = self.db.execute(
                    "SELECT o.classification FROM evidence_spans e JOIN observations o ON o.tenant=e.tenant AND o.id=e.observation WHERE e.tenant=? AND e.revision=?",
                    (tenant, dep["id"]),
                ).fetchall()
            else:
                rows = []
            found = [row[0] for row in rows] or ["internal"]
            highest = max([highest, *found], key=rank)
        return highest

    def _validate(
        self, credential: str, args: dict[str, Any], at: int
    ) -> tuple[dict[str, Any], ProviderPolicy | None, str]:
        who = self._binding(credential, args["binding"], at)
        project = args["binding"]["project"]
        request, manifest = args["request"], args["manifest"]
        if len(canonical_bytes(request)) > 98304:
            raise LocalError("inference_request_limit")
        provider = self._provider(request)
        classification = "public"
        checked: dict[str, bytes] = {}
        for parts, proofs, handles in (
            (request["messages"], manifest["segments"], False),
            (request["state_handles"], manifest["handles"], True),
        ):
            if len(parts) != len(proofs):
                raise LocalError("inference_manifest_incomplete")
            for part, proof in zip(parts, proofs, strict=True):
                data = canonical_bytes(part) if handles else part["content"].encode()
                if hashlib.sha256(data).hexdigest() != proof["sha256"]:
                    raise LocalError("inference_hash_mismatch")
                artifacts, hosts = proof["artifacts"], proof["host_authorizations"]
                if not artifacts and not hosts:
                    raise LocalError("inference_provenance_unknown")
                if handles and proof["origin"] != "derived":
                    raise LocalError("inference_provenance_unknown")
                for host in hosts:
                    if at >= host["expires_ms"]:
                        raise LocalError("host_authorization_expired")
                    classification = max(classification, host["classification"], key=rank)
                for artifact in artifacts:
                    receipt_id = artifact["receipt"]
                    if receipt_id not in checked:
                        try:
                            row = self.r._artifact(who, project, receipt_id, at)
                        except LocalError as exc:
                            raise LocalError("inference_dependency_unavailable") from exc
                        checked[receipt_id] = bytes(row["body"])
                        classification = max(
                            classification,
                            self._classification(who["tenant"], json.loads(row["dependencies"])),
                            key=rank,
                        )
                    if hashlib.sha256(checked[receipt_id]).hexdigest() != artifact["sha256"]:
                        raise LocalError("inference_hash_mismatch")
                if proof["origin"] == "host" and (
                    artifacts or len(hosts) != 1 or hosts[0]["source_hash"] != proof["sha256"]
                ):
                    raise LocalError("inference_provenance_unknown")
                if proof["origin"] == "memory" and (
                    hosts or len(artifacts) != 1 or checked[artifacts[0]["receipt"]] != data
                ):
                    raise LocalError("inference_hash_mismatch")
        # The ceiling is enforced here, before any permit exists, so a restricted source
        # can never be cited in a request the daemon authorizes for a remote provider.
        if provider is not None and rank(classification) > rank(provider.max_classification):
            raise LocalError("egress_classification_denied")
        return who, provider, classification

    def execute(self, credential: str, command: dict[str, Any]) -> Released:
        if len(canonical_bytes(command)) > 131072 or not VALIDATOR.is_valid(command):
            raise LocalError("invalid_request")
        action, args = command["action"], command["args"]
        receipt = uid("rcpt")
        with self.s.transaction() as decision:
            at = decision.evaluated_ms
            if action in {"report", "audit"}:
                raw, released, who, identifier, status = self._account(
                    credential, action, args, at, decision, receipt
                )
                self._receipt(who, receipt, raw, released, at, None)
                self.s._event(who["tenant"], "egress_" + status, identifier, at, who["principal"])
                self.s.fault("after_validation")
                released.check(self.s.clock)
                self.s._expose()
                return Released(raw, released, self.s.clock, receipt)
            who, provider, classification = self._validate(credential, args, at)
            hashes = {
                key + "_hash": canonical_hash(args[key])
                for key in ("binding", "request", "manifest")
            }
            self.db.execute(
                "DELETE FROM retries WHERE namespace='inference-v1' AND expires<=?", (at,)
            )
            if action == "authorize":
                count = self.db.execute(
                    "SELECT count(*) FROM retries WHERE tenant=? AND namespace='inference-v1'",
                    (who["tenant"],),
                ).fetchone()[0]
                if count >= 1024:
                    raise LocalError("inference_capacity")
                identifier = uid("permit")
                fields = {
                    "id": identifier,
                    "tenant": who["tenant"],
                    "principal": who["principal"],
                    "evaluated_ms": at,
                    "deadline_ms": decision.deadline_ms,
                    **hashes,
                }
                permit = self.s._token("inference-permit", **fields)
                self.db.execute(
                    "INSERT INTO retries VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        who["tenant"],
                        self.s.incarnation,
                        "inference-v1",
                        who["principal"],
                        "permit",
                        identifier,
                        canonical_hash(fields),
                        '{"state":"prepared"}',
                        None,
                        decision.deadline_ms,
                    ),
                )
                result = {"permit": permit, "status": "authorized", **fields}
                released = decision
            elif action == "consume":
                token = self.s._decode(args["permit"], "inference-permit")
                if (token["tenant"], token["principal"]) != (
                    who["tenant"],
                    who["principal"],
                ) or any(token[key] != value for key, value in hashes.items()):
                    raise LocalError("inference_permit_mismatch")
                if at < token["evaluated_ms"] or at >= token["deadline_ms"]:
                    raise LocalError("inference_permit_expired")
                key = (
                    who["tenant"],
                    self.s.incarnation,
                    "inference-v1",
                    who["principal"],
                    "permit",
                    token["id"],
                )
                row = self.db.execute(
                    "SELECT result FROM retries WHERE tenant=? AND incarnation=? AND namespace=? AND principal=? AND operation=? AND key=?",
                    key,
                ).fetchone()
                if row is None or row["result"] != '{"state":"prepared"}':
                    raise LocalError("inference_permit_consumed")
                self.db.execute(
                    "UPDATE retries SET result=? WHERE tenant=? AND incarnation=? AND namespace=? AND principal=? AND operation=? AND key=?",
                    ('{"state":"consumed"}', *key),
                )
                identifier = token["id"]
                result = {
                    "status": "consumed",
                    "id": identifier,
                    "evaluated_ms": token["evaluated_ms"],
                    "deadline_ms": token["deadline_ms"],
                    **hashes,
                }
                if provider is not None:
                    # The audit record is the fact of departure; the host completes it
                    # with a report, and a host that dies leaves it pending, truthfully.
                    policy = self.s.egress_policy
                    self.db.execute(
                        "INSERT INTO egress_audit VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            who["tenant"],
                            identifier,
                            who["principal"],
                            args["binding"]["project"],
                            self.s.incarnation,
                            provider.name,
                            args["request"]["model"],
                            provider.host,
                            str(policy.revision) if policy is not None else EGRESS_REVISION,
                            hashes["request_hash"],
                            hashes["manifest_hash"],
                            json.dumps(args["manifest"], separators=(",", ":"), sort_keys=True),
                            classification,
                            len(canonical_bytes(args["request"])),
                            at,
                            token["deadline_ms"],
                            "pending",
                            None,
                            None,
                            None,
                            None,
                            None,
                            at + RETRY_MS,
                        ),
                    )
                    result["audit"] = identifier
                released = Decision(at, decision.monotonic_ms, token["deadline_ms"])
            else:
                identifier = uid("validation")
                result = {
                    "status": "validated",
                    "id": identifier,
                    "evaluated_ms": at,
                    "deadline_ms": decision.deadline_ms,
                    **hashes,
                }
                released = decision
            raw = canonical_bytes({**result, "receipt": receipt})
            epochs = (args["binding"]["authorization_epoch"], args["binding"]["purge_epoch"])
            self._receipt(who, receipt, raw, released, at, epochs)
            self.s._event(
                who["tenant"], "inference_" + result["status"], identifier, at, who["principal"]
            )
            self.s.fault("after_validation")
            released.check(self.s.clock)
        self.s._expose()
        return Released(raw, released, self.s.clock, receipt)

    def _receipt(
        self,
        who: dict[str, Any],
        receipt: str,
        raw: bytes,
        released: Decision,
        at: int,
        epochs: tuple[int, int] | None,
    ) -> None:
        if epochs is None:
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
                released.deadline_ms,
                *epochs,
                hashlib.sha256(raw).hexdigest(),
                "[]",
            ),
        )

    def _account(
        self,
        credential: str,
        action: str,
        args: dict[str, Any],
        at: int,
        decision: Decision,
        receipt: str,
    ) -> tuple[bytes, Decision, dict[str, Any], str, str]:
        """Complete an audit record (`report`) or list a project's records (`audit`)."""
        if action == "report":
            who = self._binding(credential, args["binding"], at)
            row = self.db.execute(
                "SELECT status FROM egress_audit WHERE tenant=? AND id=? AND principal=? AND project=?",
                (who["tenant"], args["permit"], who["principal"], args["binding"]["project"]),
            ).fetchone()
            if row is None:
                raise LocalError("egress_audit_missing")
            if row["status"] != "pending":
                raise LocalError("egress_audit_reported")
            usage = args["usage"] or {}
            self.db.execute(
                "UPDATE egress_audit SET status=?,wire_hash=?,response_hash=?,input_tokens=?,output_tokens=?,reported=? WHERE tenant=? AND id=?",
                (
                    args["status"],
                    args["wire_hash"],
                    args["response_hash"],
                    usage.get("input_tokens"),
                    usage.get("output_tokens"),
                    at,
                    who["tenant"],
                    args["permit"],
                ),
            )
            result = {"status": "reported", "permit": args["permit"], "outcome": args["status"]}
            raw = canonical_bytes({**result, "receipt": receipt})
            return raw, decision, who, args["permit"], args["status"]
        project = identifier(args["project"])
        who = self.s._auth(credential, "inference", project, at)
        self.s._auth(credential, "read", project, at)
        params: list[Any] = [who["tenant"], project, at]
        cursor = ""
        if args["before"] is not None:
            cursor = " AND evaluated<?"
            params.append(args["before"])
        rows = self.db.execute(
            "SELECT * FROM egress_audit WHERE tenant=? AND project=? AND expires>?"
            + cursor
            + " ORDER BY evaluated DESC,id DESC LIMIT ?",
            (*params, AUDIT_PAGE + 1),
        ).fetchall()
        items = []
        for row in rows[:AUDIT_PAGE]:
            manifest = json.loads(row["manifest"])
            receipts = sorted(
                {
                    a["receipt"]
                    for proof in [*manifest["segments"], *manifest["handles"]]
                    for a in proof["artifacts"]
                }
            )
            items.append(
                {
                    "permit": row["id"],
                    "principal": row["principal"],
                    "provider": row["provider"],
                    "model": row["model"],
                    "host": row["host"],
                    "policy_revision": row["policy_revision"],
                    "status": row["status"],
                    "classification": row["classification"],
                    "request_bytes": row["request_bytes"],
                    "request_hash": row["request_hash"],
                    "manifest_hash": row["manifest_hash"],
                    "wire_hash": row["wire_hash"],
                    "response_hash": row["response_hash"],
                    "input_tokens": row["input_tokens"],
                    "output_tokens": row["output_tokens"],
                    "segments": len(manifest["segments"]),
                    "receipts": receipts,
                    "evaluated_ms": row["evaluated"],
                    "reported_ms": row["reported"],
                    "expires_ms": row["expires"],
                }
            )
        result = {
            "status": "listed",
            "items": items,
            "next_before": rows[AUDIT_PAGE - 1]["evaluated"] if len(rows) > AUDIT_PAGE else None,
            "policy_revision": self.s.egress_revision(),
        }
        return canonical_bytes({**result, "receipt": receipt}), decision, who, "listing", "listed"
