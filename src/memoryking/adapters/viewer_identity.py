"""Owner-only viewer policy on the existing independent security journal.

No provider calls, browser cookies, owner-token substitution or model tool registration.
Both stores serialize through their canonical transactions and replay this projection.
"""

from __future__ import annotations

import json
from typing import Any

from memoryking.adapters.local_store import MAX_COUNTER, LocalStore
from memoryking.contracts.canonical import canonical_bytes, canonical_hash
from memoryking.domain.local import LocalError, temporal_valid
from memoryking.domain.viewer import GITHUB_ISSUER, VIEWER_CAPABILITIES, VerifiedViewer


class ViewerIdentity:
    def __init__(self, store: LocalStore):
        self.s, self.db = store, store.db

    def execute(self, credential: str, command: dict[str, Any]) -> dict[str, Any]:
        from memoryking.contracts.viewer import validate_viewer

        validate_viewer("ViewerAdminCommand", command)
        if command["incarnation"] != self.s.incarnation:
            raise LocalError("history_changed")
        with self.s.transaction() as decision:
            who = self.s._auth(credential, "admin", "*", decision.evaluated_ms)
            if command["action"] == "list":
                result = self._list(who["tenant"])
            else:
                result = self._mutate(who, command, decision.evaluated_ms)
        self.s._expose()
        decision.check(self.s.clock)
        return result

    def _list(self, tenant: str) -> dict[str, Any]:
        rows = self.db.execute(
            "SELECT * FROM viewer_bindings WHERE tenant=? ORDER BY principal LIMIT 257", (tenant,)
        ).fetchall()
        grants = self.db.execute(
            "SELECT * FROM viewer_grants WHERE tenant=? ORDER BY principal,project LIMIT 1025",
            (tenant,),
        ).fetchall()
        return {
            "bindings": [dict(row) for row in rows[:256]],
            "grants": [dict(row) for row in grants[:1024]],
            "truncated": len(rows) > 256 or len(grants) > 1024,
        }

    def _binding(self, tenant: str, principal: str) -> Any:
        return self.db.execute(
            "SELECT * FROM viewer_bindings WHERE tenant=? AND principal=?", (tenant, principal)
        ).fetchone()

    def _mutate(self, who: dict[str, Any], command: dict[str, Any], at: int) -> dict[str, Any]:
        tenant, actor, key = who["tenant"], who["principal"], command["key"]
        request_hash = canonical_hash(command)
        prior = self.db.execute(
            "SELECT request_hash,result FROM viewer_admin_retries WHERE tenant=? AND actor=? AND key=?",
            (tenant, actor, key),
        ).fetchone()
        if prior is not None:
            if prior["request_hash"] != request_hash:
                raise LocalError("idempotency_conflict")
            return dict(json.loads(prior["result"]))
        action, args = command["action"], command["args"]
        principal = args["principal"]
        row = self._binding(tenant, principal)
        if action == "bind":
            if (
                row is not None
                or self.db.execute(
                    "SELECT 1 FROM grants WHERE tenant=? AND principal=?", (tenant, principal)
                ).fetchone()
            ):
                raise LocalError("viewer_binding_conflict")
            count = self.db.execute(
                "SELECT count(*) FROM viewer_bindings WHERE tenant=?", (tenant,)
            ).fetchone()[0]
            if count >= 256:
                raise LocalError("viewer_limit")
        else:
            if row is None:
                raise LocalError("unavailable")
            if row["version"] != args["expected_version"]:
                raise LocalError("revision_conflict")
            if max(row["version"], row["generation"]) >= MAX_COUNTER:
                raise LocalError("counter_exhausted")
            if action == "grant" and not row["enabled"]:
                raise LocalError("denied")
        if (
            action in {"bind", "rebind"}
            and self.db.execute(
                "SELECT 1 FROM viewer_bindings WHERE tenant=? AND issuer=? AND user_id=? AND principal<>?",
                (tenant, GITHUB_ISSUER, args["user_id"], principal),
            ).fetchone()
        ):
            raise LocalError("viewer_binding_conflict")
        if action == "grant":
            starts, expires = args["starts"], args["expires"]
            if starts is not None and expires is not None and starts >= expires:
                raise LocalError("invalid_interval")
            count = self.db.execute(
                "SELECT count(*) FROM viewer_grants WHERE tenant=?", (tenant,)
            ).fetchone()[0]
            existing = self.db.execute(
                "SELECT 1 FROM viewer_grants WHERE tenant=? AND principal=? AND project=?",
                (tenant, principal, args["project"]),
            ).fetchone()
            if count >= 1024 and not existing:
                raise LocalError("viewer_limit")
        result = {
            "principal": principal,
            "version": 1 if row is None else row["version"] + 1,
            "generation": 1 if row is None else row["generation"] + 1,
            "action": action,
        }
        policy = {
            "kind": "viewer",
            "tenant": tenant,
            "actor": actor,
            "principal": principal,
            "operation": action,
            "args": args,
            "key": key,
            "request_hash": request_hash,
            "result": result,
        }
        record = self.s._journal(policy)
        self.s._apply(record, at)
        return result

    def apply_policy(self, action: dict[str, Any]) -> None:
        """Replay only through LocalStore._apply's ordered, authenticated journal."""
        tenant, principal = action["tenant"], action["principal"]
        operation, args, result = action["operation"], action["args"], action["result"]
        if operation == "bind":
            self.db.execute(
                "INSERT INTO viewer_bindings VALUES (?,?,?,?,?,?,?)",
                (tenant, principal, GITHUB_ISSUER, args["user_id"], 1, 1, 1),
            )
        else:
            self.db.execute(
                "UPDATE viewer_bindings SET generation=?,version=? WHERE tenant=? AND principal=?",
                (result["generation"], result["version"], tenant, principal),
            )
        if operation == "rebind":
            self.db.execute(
                "UPDATE viewer_bindings SET user_id=? WHERE tenant=? AND principal=?",
                (args["user_id"], tenant, principal),
            )
        if operation == "disable":
            self.db.execute(
                "UPDATE viewer_bindings SET enabled=0 WHERE tenant=? AND principal=?",
                (tenant, principal),
            )
        if operation in {"rebind", "disable"}:
            self.db.execute(
                "DELETE FROM viewer_grants WHERE tenant=? AND principal=?", (tenant, principal)
            )
            self.db.execute(
                "DELETE FROM grants WHERE tenant=? AND principal=?", (tenant, principal)
            )
        if operation in {"grant", "revoke"}:
            project = args["project"]
            self.db.execute(
                "DELETE FROM viewer_grants WHERE tenant=? AND principal=? AND project=?",
                (tenant, principal, project),
            )
            self.db.execute(
                "DELETE FROM grants WHERE tenant=? AND principal=? AND project=?",
                (tenant, principal, project),
            )
            if operation == "grant":
                self.db.execute(
                    "INSERT INTO viewer_grants VALUES (?,?,?,?,?)",
                    (tenant, principal, project, args["starts"], args["expires"]),
                )
                self.db.execute(
                    "INSERT INTO grants VALUES (?,?,?,?,?,?)",
                    (tenant, principal, project, '["read"]', args["starts"], args["expires"]),
                )
        self.db.execute(
            "INSERT INTO viewer_admin_retries VALUES (?,?,?,?,?)",
            (
                tenant,
                action["actor"],
                action["key"],
                action["request_hash"],
                canonical_bytes(result).decode(),
            ),
        )

    def resolve_authenticated_subject(self, issuer: str, user_id: str) -> VerifiedViewer:
        """Trusted provider-adapter call only; never expose subject selection in a route.

        M04 must establish provider identity first. This lookup is not a login API.
        The returned internal credential is read-only and current-policy bound.
        """
        if issuer != GITHUB_ISSUER:
            raise LocalError("denied")
        with self.s.transaction() as decision:
            row = self.db.execute(
                "SELECT * FROM viewer_bindings WHERE tenant=? AND issuer=? AND user_id=? AND enabled=1",
                (self.s.tenant if hasattr(self.s, "tenant") else self._tenant(), issuer, user_id),
            ).fetchone()
            if row is None:
                raise LocalError("denied")
            epoch = self.db.execute(
                "SELECT policy_epoch FROM tenants WHERE tenant=?", (row["tenant"],)
            ).fetchone()[0]
            token = self.s._token(
                "credential",
                tenant=row["tenant"],
                principal=row["principal"],
                viewer={"user_id": user_id, "generation": row["generation"], "policy_epoch": epoch},
            )
            result = VerifiedViewer(
                self.s.store_id,
                row["tenant"],
                row["principal"],
                user_id,
                self.s.incarnation,
                row["generation"],
                epoch,
                token,
            )
        self.s._expose()
        decision.check(self.s.clock)
        return result

    def _tenant(self) -> str:
        rows = self.db.execute("SELECT tenant FROM tenants LIMIT 2").fetchall()
        if len(rows) != 1:
            raise LocalError("viewer_tenant_configuration_required")
        return str(rows[0][0])

    def check_identity(
        self, identity: dict[str, Any], capability: str, project: str, at: int
    ) -> None:
        row = self._binding(identity["tenant"], identity["principal"])
        marker = identity.get("viewer")
        if row is None:
            if marker is not None:
                raise LocalError("denied")
            return
        epoch = self.db.execute(
            "SELECT policy_epoch FROM tenants WHERE tenant=?", (identity["tenant"],)
        ).fetchone()[0]
        if (
            capability not in VIEWER_CAPABILITIES
            or not isinstance(marker, dict)
            or not row["enabled"]
            or marker
            != {"user_id": row["user_id"], "generation": row["generation"], "policy_epoch": epoch}
        ):
            raise LocalError("denied")
        grant = self.db.execute(
            "SELECT starts,expires FROM viewer_grants WHERE tenant=? AND principal=? AND project=?",
            (identity["tenant"], identity["principal"], project),
        ).fetchone()
        if grant is None or not temporal_valid(at, grant["starts"], grant["expires"]):
            raise LocalError("denied")
