"""Exchange-only, subject-authorized observation reads. No browser authentication.

Both storage adapters use these operations and portable SQL. Native credentials,
native reads and issuer-acting delegation retain their existing semantics.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from memoryking.application.retention import index_receipt
from memoryking.contracts.canonical import canonical_bytes, canonical_hash
from memoryking.domain.local import LocalError, Released, is_user_project, temporal_valid

if TYPE_CHECKING:
    from memoryking.application.store_core import StoreCore

AUDIENCE = "clankercity-viewer-read-v1"
PROVIDER = "github.com"
HEX = re.compile(r"[0-9a-f]{64}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")
GITHUB_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
LEASE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
OPERATIONS = frozenset({"viewer_exchange", "viewer_read", "viewer_close"})
READ_KEYS = (
    "id",
    "authority",
    "assurance",
    "source",
    "source_event",
    "principal",
    "project",
    "text",
    "position",
    "origin",
    "classification",
    "observed_at_ms",
)


def is_operation(value: Any) -> bool:
    """Whether a request's `operation` names a delegated read. The value is whatever JSON the
    caller sent: a list or an object is not hashable, so its type is checked before the set."""
    return isinstance(value, str) and value in OPERATIONS


def require(ok: Any, code: str = "input_invalid") -> None:
    if not ok:
        raise LocalError(code)


def identifier(value: Any) -> str:
    require(isinstance(value, str) and IDENTIFIER.fullmatch(value))
    return str(value)


class Viewer:
    def __init__(self, store: StoreCore):
        self.s = store

    def _epochs(self, tenant: str) -> tuple[int, int]:
        row = self.s.db.execute(
            "SELECT policy_epoch,purge_epoch FROM tenants WHERE tenant=?", (tenant,)
        ).fetchone()
        return int(row[0]), int(row[1])

    def _subject(self, tenant: str, subject: str) -> None:
        # Role domains remain disjoint even if a row is disabled or its grant expired.
        require(
            not self.s.db.execute(
                "SELECT 1 FROM viewer_registries WHERE tenant=? AND delegator=? LIMIT 1",
                (tenant, subject),
            ).fetchone(),
            "denied",
        )
        for row in self.s.db.execute(
            "SELECT capabilities FROM grants WHERE tenant=? AND principal=?", (tenant, subject)
        ).fetchall():
            require(not {"admin", "viewer_exchange"}.intersection(json.loads(row[0])), "denied")

    def configure(self, credential: str, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        fields = {"installation_id", "provider", "starts", "expires", "enabled"}
        fields |= (
            {"delegator", "project", "audience"} if kind == "registry" else {"github_id", "subject"}
        )
        require(kind in {"registry", "mapping"} and isinstance(body, dict) and set(body) == fields)
        identifier(body["installation_id"])
        require(body["provider"] == PROVIDER and type(body["enabled"]) is bool)
        for name in ("starts", "expires"):
            require(
                body[name] is None
                or type(body[name]) is int
                and 0 <= body[name] <= 9_007_199_254_740_991
            )
        require(
            body["starts"] is None or body["expires"] is None or body["starts"] < body["expires"]
        )
        if kind == "registry":
            identifier(body["delegator"])
            identifier(body["project"])
            require(
                not is_user_project(body["project"])
                and body["project"] != "*"
                and body["audience"] == AUDIENCE
            )
            keys = (body["installation_id"], body["delegator"], body["project"])
            clause = "installation=? AND delegator=? AND project=?"
        else:
            identifier(body["subject"])
            require(isinstance(body["github_id"], str) and GITHUB_ID.fullmatch(body["github_id"]))
            keys = (body["installation_id"], body["provider"], body["github_id"])
            clause = "installation=? AND provider=? AND github_id=?"
        with self.s.transaction() as decision:
            who = self.s._auth(credential, "admin", "*", decision.evaluated_ms)
            if kind == "mapping":
                self._subject(who["tenant"], body["subject"])
            else:
                require(
                    not self.s.db.execute(
                        "SELECT 1 FROM viewer_mappings WHERE tenant=? AND subject=? LIMIT 1",
                        (who["tenant"], body["delegator"]),
                    ).fetchone(),
                    "denied",
                )
            old = self.s.db.execute(
                "SELECT revision FROM viewer_"
                + ("registries" if kind == "registry" else "mappings")
                + " WHERE tenant=? AND "
                + clause,
                (who["tenant"], *keys),
            ).fetchone()
            revision = int(old[0]) + 1 if old else 1
            action = {
                "kind": "viewer_" + kind,
                "actor": who["principal"],
                "tenant": who["tenant"],
                "body": body,
                "revision": revision,
            }
            self.s._apply(self.s._journal(action), decision.evaluated_ms)
        self.s._expose()
        return {"version": 1, "revision": revision}

    def apply(self, action: dict[str, Any]) -> None:
        b, tenant, revision = action["body"], action["tenant"], action["revision"]
        if action["kind"] == "viewer_registry":
            self.s.db.execute(
                "INSERT INTO viewer_registries(tenant,installation,delegator,project,provider,audience,revision,enabled,starts,expires) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(tenant,installation,delegator,project) DO UPDATE SET provider=EXCLUDED.provider,audience=EXCLUDED.audience,revision=EXCLUDED.revision,enabled=EXCLUDED.enabled,starts=EXCLUDED.starts,expires=EXCLUDED.expires",
                (
                    tenant,
                    b["installation_id"],
                    b["delegator"],
                    b["project"],
                    b["provider"],
                    b["audience"],
                    revision,
                    int(b["enabled"]),
                    b["starts"],
                    b["expires"],
                ),
            )
        else:
            self.s.db.execute(
                "INSERT INTO viewer_mappings(tenant,installation,provider,github_id,subject,revision,enabled,starts,expires) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(tenant,installation,provider,github_id) DO UPDATE SET subject=EXCLUDED.subject,revision=EXCLUDED.revision,enabled=EXCLUDED.enabled,starts=EXCLUDED.starts,expires=EXCLUDED.expires",
                (
                    tenant,
                    b["installation_id"],
                    b["provider"],
                    b["github_id"],
                    b["subject"],
                    revision,
                    int(b["enabled"]),
                    b["starts"],
                    b["expires"],
                ),
            )

    def _backend(self, credential: str, at: int) -> dict[str, Any]:
        require(isinstance(credential, str) and 1 <= len(credential) <= 8192, "invalid_token")
        who = self.s._decode(credential, "credential")
        require(not who.get("delegation"), "denied")
        # SharedStore's bound-principal/audience guards still apply via _auth below.
        rows = self.s.db.execute(
            "SELECT * FROM grants WHERE tenant=? AND principal=?", (who["tenant"], who["principal"])
        ).fetchall()
        require(
            rows and all(set(json.loads(r["capabilities"])) == {"viewer_exchange"} for r in rows),
            "denied",
        )
        grant = next((r for r in rows if temporal_valid(at, r["starts"], r["expires"])), None)
        if grant is None:
            raise LocalError("denied")
        try:
            return self.s._auth(credential, "viewer_exchange", grant["project"], at)
        except LocalError as exc:
            raise LocalError(exc.code) from None

    def _authority(
        self, who: dict[str, Any], installation: str, project: str, github_id: str, at: int
    ) -> tuple[Any, Any, int]:
        tenant, backend = who["tenant"], who["principal"]
        require(not is_user_project(project) and project != "*", "denied")
        registry = self.s.db.execute(
            "SELECT * FROM viewer_registries WHERE tenant=? AND installation=? AND delegator=? AND project=?",
            (tenant, installation, backend, project),
        ).fetchone()
        mapping = self.s.db.execute(
            "SELECT * FROM viewer_mappings WHERE tenant=? AND installation=? AND provider=? AND github_id=?",
            (tenant, installation, PROVIDER, github_id),
        ).fetchone()
        require(registry is not None and mapping is not None, "denied")
        for row in (registry, mapping):
            require(
                row["enabled"] == 1 and temporal_valid(at, row["starts"], row["expires"]), "denied"
            )
        require(registry["audience"] == AUDIENCE and registry["provider"] == PROVIDER, "denied")
        self._subject(tenant, mapping["subject"])
        require(mapping["subject"] != backend, "denied")
        subject = self.s.db.execute(
            "SELECT * FROM grants WHERE tenant=? AND principal=? AND project=?",
            (tenant, mapping["subject"], project),
        ).fetchone()
        require(
            subject is not None
            and "read" in json.loads(subject["capabilities"])
            and temporal_valid(at, subject["starts"], subject["expires"]),
            "denied",
        )
        grants = self.s.db.execute(
            "SELECT * FROM grants WHERE tenant=? AND principal=? AND project IN (?, '*')",
            (tenant, backend, project),
        ).fetchall()
        valid = [r for r in grants if temporal_valid(at, r["starts"], r["expires"])]
        require(valid, "denied")
        # A grant which authorizes this evaluation sets the bound; no union extends it.
        backend_end = max((r["expires"] if r["expires"] is not None else at + 30000) for r in valid)
        ends = [at + 30000, backend_end]
        ends += [r["expires"] for r in (registry, mapping, subject) if r["expires"] is not None]
        return registry, mapping, min(ends)

    @staticmethod
    def _shape(operation: str, args: Any) -> None:
        expected = {
            "viewer_exchange": {
                "installation_id",
                "audience",
                "project",
                "provider",
                "github_id",
                "session_ref",
            },
            "viewer_read": {"lease_id", "session_ref", "query", "cursor"},
            "viewer_close": {"lease_id", "session_ref"},
        }
        require(
            operation in expected and isinstance(args, dict) and set(args) == expected[operation]
        )
        require(
            all(isinstance(v, str) for v in args.values()) and HEX.fullmatch(args["session_ref"])
        )
        if operation == "viewer_exchange":
            identifier(args["installation_id"])
            identifier(args["project"])
            require(
                args["provider"] == PROVIDER
                and args["audience"] == AUDIENCE
                and GITHUB_ID.fullmatch(args["github_id"])
            )
        else:
            handle = args["lease_id"]
            require(LEASE.fullmatch(handle), "lease_invalid")
            decoded = base64.urlsafe_b64decode(handle + "=")
            require(
                len(decoded) == 32
                and base64.urlsafe_b64encode(decoded).decode().rstrip("=") == handle,
                "lease_invalid",
            )
            if operation == "viewer_read":
                require(len(args["query"]) <= 256 and len(args["cursor"].encode()) <= 4096)

    def execute(self, credential: str, operation: str, args: dict[str, Any]) -> Released:
        with self.s.viewer_transaction() as decision:
            at = decision.evaluated_ms
            who = self._backend(credential, at)
            require(len(canonical_bytes(args)) <= 16384, "bounds_exceeded")
            self._shape(operation, args)
            if operation == "viewer_exchange":
                self.s.viewer_lock(who["tenant"], None, metadata=True)
                result, end = self._exchange(who, args, at)
            else:
                handle_hash = hashlib.sha256(args["lease_id"].encode()).hexdigest()
                self.s.viewer_lock(who["tenant"], handle_hash, metadata=operation == "viewer_close")
                row = self.s.db.execute(
                    "SELECT * FROM viewer_leases WHERE tenant=? AND lease_hash=?",
                    (who["tenant"], handle_hash),
                ).fetchone()
                require(row is not None, "lease_invalid")
                lease = json.loads(row["body"])
                require(
                    lease["delegator"] == who["principal"]
                    and lease["delegator_generation"]
                    == self.s._generation(who["tenant"], who["principal"])
                    and lease["session_ref"] == args["session_ref"],
                    "lease_invalid",
                )
                require(
                    lease["store"] == self.s.store_id
                    and lease["incarnation"] == self.s.incarnation
                    and lease["audience"] == AUDIENCE,
                    "lease_invalid",
                )
                if operation == "viewer_close":
                    self.s.db.execute(
                        "UPDATE viewer_leases SET closed=1 WHERE tenant=? AND lease_hash=?",
                        (who["tenant"], handle_hash),
                    )
                    result, end = {"version": 1, "closed": True}, decision.deadline_ms
                else:
                    require(not row["closed"], "lease_invalid")
                    require(
                        at < row["expires"], "lease_invalid" if args["cursor"] else "lease_expired"
                    )
                    require(
                        self._epochs(who["tenant"])
                        == (lease["policy_epoch"], lease["purge_epoch"]),
                        "lease_invalid",
                    )
                    registry, mapping, bound = self._authority(
                        who, lease["installation"], lease["project"], lease["github_id"], at
                    )
                    require(
                        (
                            registry["revision"],
                            mapping["revision"],
                            mapping["subject"],
                            self.s._generation(who["tenant"], mapping["subject"]),
                            *self._epochs(who["tenant"]),
                        )
                        == (
                            lease["registry_revision"],
                            lease["mapping_revision"],
                            lease["subject"],
                            lease["subject_generation"],
                            lease["policy_epoch"],
                            lease["purge_epoch"],
                        ),
                        "lease_invalid",
                    )
                    end = min(row["expires"], bound, decision.deadline_ms)
                    result = self._read(
                        who, lease, handle_hash, args, decision, end, min(row["expires"], bound)
                    )
            decision = replace(decision, deadline_ms=min(decision.deadline_ms, end))
            self.s.fault("after_validation")
        self.s._expose()
        decision.check(self.s.clock)
        return Released(
            canonical_bytes(result), decision, self.s.clock, result.get("access_receipt")
        )

    def _exchange(
        self, who: dict[str, Any], args: dict[str, Any], at: int
    ) -> tuple[dict[str, Any], int]:
        registry, mapping, end = self._authority(
            who, args["installation_id"], args["project"], args["github_id"], at
        )
        tenant = who["tenant"]
        self.s.db.execute(
            "DELETE FROM viewer_leases WHERE tenant=? AND lease_hash IN (SELECT lease_hash FROM viewer_leases WHERE tenant=? AND (expires<=? OR closed=1) ORDER BY expires LIMIT 64)",
            (tenant, tenant, at),
        )
        for clause, values, limit in [
            ("", (), 1024),
            (" AND delegator=?", (who["principal"],), 256),
            (" AND subject=? AND session_ref=?", (mapping["subject"], args["session_ref"]), 16),
        ]:
            count = self.s.db.execute(
                "SELECT count(*) FROM viewer_leases WHERE tenant=? AND closed=0 AND expires>?"
                + clause,
                (tenant, at, *values),
            ).fetchone()[0]
            require(count < limit, "service_busy")
        handle = secrets.token_urlsafe(32)
        digest = hashlib.sha256(handle.encode()).hexdigest()
        policy, purge = self._epochs(tenant)
        lease = {
            "tenant": tenant,
            "created_ms": at,
            "store": self.s.store_id,
            "incarnation": self.s.incarnation,
            "audience": AUDIENCE,
            "installation": args["installation_id"],
            "project": args["project"],
            "provider": PROVIDER,
            "github_id": args["github_id"],
            "session_ref": args["session_ref"],
            "delegator": who["principal"],
            "delegator_generation": self.s._generation(tenant, who["principal"]),
            "subject": mapping["subject"],
            "subject_generation": self.s._generation(tenant, mapping["subject"]),
            "registry_revision": registry["revision"],
            "mapping_revision": mapping["revision"],
            "policy_epoch": policy,
            "purge_epoch": purge,
        }
        self.s.db.execute(
            "INSERT INTO viewer_leases(tenant,lease_hash,delegator,subject,session_ref,expires,closed,body) VALUES(?,?,?,?,?,?,0,?)",
            (
                tenant,
                digest,
                who["principal"],
                mapping["subject"],
                args["session_ref"],
                end,
                canonical_bytes(lease).decode(),
            ),
        )
        return {
            "version": 1,
            "lease_id": handle,
            "incarnation": self.s.incarnation,
            "policy_epoch": policy,
            "purge_epoch": purge,
            "evaluated_ms": at,
            "expires_ms": end,
            "valid_for_ms": end - at,
        }, end

    def _read(
        self,
        who: dict[str, Any],
        lease: dict[str, Any],
        digest: str,
        args: dict[str, Any],
        decision: Any,
        end: int,
        display_end: int,
    ) -> dict[str, Any]:
        from memoryking.application.store_core import uid

        tenant, at = who["tenant"], decision.evaluated_ms
        subject = {"tenant": tenant, "principal": lease["subject"]}
        context = canonical_hash(
            {
                k: lease[k]
                for k in (
                    "tenant",
                    "subject",
                    "project",
                    "session_ref",
                    "policy_epoch",
                    "purge_epoch",
                    "mapping_revision",
                )
            }
        )
        query_hash = hashlib.sha256(args["query"].encode()).hexdigest()
        after = 0
        if args["cursor"]:
            try:
                cursor = self.s._decode(args["cursor"], "viewer_cursor")
                require(
                    set(cursor)
                    == {
                        "kind",
                        "store",
                        "incarnation",
                        "lease_hash",
                        "context_sha256",
                        "query_sha256",
                        "position",
                    },
                    "lease_invalid",
                )
                require(
                    (cursor["lease_hash"], cursor["context_sha256"], cursor["query_sha256"])
                    == (digest, context, query_hash)
                    and type(cursor["position"]) is int
                    and cursor["position"] >= 0,
                    "lease_invalid",
                )
                after = cursor["position"]
            except LocalError:
                raise LocalError("lease_invalid") from None
        sql = "SELECT o.* FROM observations o WHERE tenant=? AND project=? AND position>? AND deleted=0"
        values: list[Any] = [tenant, lease["project"], after]
        if args["query"]:
            sql += (
                " AND id IN (SELECT id FROM lexical WHERE tenant=? AND "
                + self.s.dialect.lexical_match()
                + ")"
            )
            values.extend([tenant, '"' + args["query"].replace('"', '""') + '"'])
        rows = self.s.db.execute(sql + " ORDER BY position LIMIT 32", values).fetchall()
        receipt = uid("rcpt")

        def token(position: int) -> str:
            return self.s._token(
                "viewer_cursor",
                lease_hash=digest,
                context_sha256=context,
                query_sha256=query_hash,
                position=position,
            )

        def envelope(
            items: list[dict[str, Any]], oversize: list[dict[str, Any]], position: int
        ) -> dict[str, Any]:
            more = (
                self.s.db.execute(
                    sql + " ORDER BY position LIMIT 1",
                    [tenant, lease["project"], position, *values[3:]],
                ).fetchone()
                is not None
            )
            return {
                "version": 1,
                "data": {"items": items},
                "cursor": token(position) if more else "",
                "oversize": oversize,
                "access_receipt": receipt,
                "incarnation": self.s.incarnation,
                "policy_epoch": lease["policy_epoch"],
                "evaluated_ms": at,
                "release_for_ms": max(0, end - at),
                "display_for_ms": min(15000, display_end - at),
            }

        items: list[dict[str, Any]] = []
        omitted: list[dict[str, Any]] = []
        position = after
        for row in rows:
            if not self.s._visible(subject, row, at):
                position = row["position"]
                continue
            item = self.s.observation_item(subject, row, handle=False)
            metadata_ok = all(
                not isinstance(value, str)
                or len(value.encode()) <= (131072 if key == "text" else 16384)
                for key, value in item.items()
            )
            require(set(item) == set(READ_KEYS), "invalid_response")
            require(
                all(
                    type(item[k]) is int and item[k] >= 0
                    if k in {"position", "observed_at_ms"}
                    else isinstance(item[k], str)
                    for k in READ_KEYS
                ),
                "invalid_response",
            )
            single = envelope([item], [], row["position"])
            if not metadata_ok or len(canonical_bytes(single)) > 262144:
                position = row["position"]
                omitted = [{"position": position, "code": "item_bounds_exceeded"}]
                break
            tentative = envelope(items + [item], [], row["position"])
            # Reserve the tiny typed omission before filling the page.
            reserve = (
                len(
                    canonical_bytes(
                        {"position": 9_007_199_254_740_991, "code": "item_bounds_exceeded"}
                    )
                )
                + 2
            )
            if (
                sum(len(i["text"].encode()) for i in items + [item]) > 131072
                or len(canonical_bytes(tentative)) + reserve > 262144
            ):
                if not items:
                    position = row["position"]
                    omitted = [{"position": position, "code": "item_bounds_exceeded"}]
                break
            items.append(item)
            position = row["position"]
        result = envelope(items, omitted, position)
        require(len(canonical_bytes(result)) <= 262144, "bounds_exceeded")
        self.s.db.execute(
            "INSERT INTO receipts(tenant,id,principal,incarnation,evaluated,deadline,policy_epoch,purge_epoch,response_hash,observation_ids) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                tenant,
                receipt,
                lease["subject"],
                self.s.incarnation,
                at,
                end,
                lease["policy_epoch"],
                lease["purge_epoch"],
                canonical_hash(result),
                json.dumps([i["id"] for i in items]),
            ),
        )
        index_receipt(self.s.db, tenant, receipt)
        self.s.db.execute(
            "INSERT INTO viewer_receipt_context(tenant,receipt,context) VALUES(?,?,?)",
            (tenant, receipt, canonical_bytes({**lease, "lease_hash": digest}).decode()),
        )
        return result
