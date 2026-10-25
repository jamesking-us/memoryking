"""Exact-request authorization for the trusted, local-only reference dispatcher."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from memoryking.adapters.local_store import LocalStore, uid
from memoryking.adapters.retrieval import Retrieval
from memoryking.contracts.canonical import canonical_bytes, canonical_hash
from memoryking.domain.local import Decision, LocalError, Released

VALIDATOR = Draft202012Validator(
    json.loads(Path(__file__).with_name("inference.schema.json").read_text())
)
EGRESS_REVISION = "local-recording-v1"


class Inference:
    """A credential with inference capability attests trusted host provenance.

    The server verifies byte coverage and every referenced MemoryKing artifact. It
    cannot infer whether an arbitrary host lied about a derived segment's origins.
    The ordinary model-facing tools deliberately cannot invoke this surface.
    """

    def __init__(self, store: LocalStore):
        self.s, self.db = store, store.db
        self.r = Retrieval(store)

    def _validate(self, credential: str, args: dict[str, Any], at: int) -> dict[str, Any]:
        binding = args["binding"]
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
            "egress_policy_revision": EGRESS_REVISION,
        }
        if any(binding[key] != value for key, value in expected.items()):
            raise LocalError("inference_context_changed")
        request, manifest = args["request"], args["manifest"]
        if len(canonical_bytes(request)) > 98304:
            raise LocalError("inference_request_limit")
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
                for artifact in artifacts:
                    identifier = artifact["receipt"]
                    if identifier not in checked:
                        try:
                            row = self.r._artifact(who, project, identifier, at)
                        except LocalError as exc:
                            raise LocalError("inference_dependency_unavailable") from exc
                        checked[identifier] = bytes(row["body"])
                    if hashlib.sha256(checked[identifier]).hexdigest() != artifact["sha256"]:
                        raise LocalError("inference_hash_mismatch")
                if proof["origin"] == "host" and (
                    artifacts or len(hosts) != 1 or hosts[0]["source_hash"] != proof["sha256"]
                ):
                    raise LocalError("inference_provenance_unknown")
                if proof["origin"] == "memory" and (
                    hosts or len(artifacts) != 1 or checked[artifacts[0]["receipt"]] != data
                ):
                    raise LocalError("inference_hash_mismatch")
        return who

    def execute(self, credential: str, command: dict[str, Any]) -> Released:
        if len(canonical_bytes(command)) > 131072 or not VALIDATOR.is_valid(command):
            raise LocalError("invalid_request")
        action, args = command["action"], command["args"]
        receipt = uid("rcpt")
        with self.s.transaction() as decision:
            at = decision.evaluated_ms
            who = self._validate(credential, args, at)
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
            self.s._event(
                who["tenant"], "inference_" + result["status"], identifier, at, who["principal"]
            )
            self.s.fault("after_validation")
            released.check(self.s.clock)
        self.s._expose()
        return Released(raw, released, self.s.clock, receipt)
