"""Viewer and report extensions shared by the private SQLite and PostgreSQL adapters."""

from __future__ import annotations

from typing import Any

from memoryking.application.store_core import CAPABILITIES, StoreCore
from memoryking.domain.local import LocalError
from memoryking.domain.reporting import REPORT_CAPABILITIES


class MobileStoreCore(StoreCore):
    capabilities = CAPABILITIES | REPORT_CAPABILITIES

    def _auth(self, token: str, capability: str, project: str, at: int) -> dict[str, Any]:
        from memoryking.adapters.viewer_identity import ViewerIdentity

        identity = super()._auth(token, capability, project, at)
        ViewerIdentity(self).check_identity(identity, capability, project, at)
        return identity

    def _subject_token(self, who: dict[str, Any]) -> str:
        fields = {key: who[key] for key in ("tenant", "principal")}
        if "viewer" in who:
            fields["viewer"] = who["viewer"]
        return self._token("credential", **fields)

    def _journal(self, action: dict[str, Any]) -> dict[str, Any]:
        # Called inside the canonical policy transaction, before durable intent.
        if (
            action["kind"] in {"grant", "revoke"}
            and self.db.execute(
                "SELECT 1 FROM viewer_bindings WHERE tenant=? AND principal=?",
                (action["tenant"], action["principal"]),
            ).fetchone()
        ):
            raise LocalError("viewer_policy_operation_required")
        return super()._journal(action)

    def _apply(self, record: dict[str, Any], at: int) -> None:
        action = record["action"]
        kind = action["kind"]
        if kind not in {"viewer", "report_policy", "report_delivery"}:
            super()._apply(record, at)
            return
        sequence = int(self.db.execute("SELECT value FROM meta WHERE key='journal'").fetchone()[0])
        if record["sequence"] <= sequence:
            return
        tenant = action["tenant"]
        if self.db.execute("SELECT 1 FROM tenants WHERE tenant=?", (tenant,)).fetchone() is None:
            raise LocalError("journal_tenant_missing")
        if kind == "viewer":
            from memoryking.adapters.viewer_identity import ViewerIdentity

            ViewerIdentity(self).apply_policy(action)
        elif kind == "report_policy":
            from memoryking.adapters.report_store import Reports

            Reports(self).apply_control(action)
        else:
            from memoryking.adapters.report_delivery import Deliveries

            Deliveries(self).apply(action)
        if kind != "report_delivery":
            self.db.execute(
                "UPDATE tenants SET policy_epoch=policy_epoch+1 WHERE tenant=?", (tenant,)
            )
        position = self._event(
            tenant,
            kind,
            action.get("observation", action.get("operation", action.get("principal", "policy"))),
            at,
            action.get("actor", "system"),
        )
        self._enqueue(tenant, position, None, "system")
        self.db.execute("UPDATE meta SET value=? WHERE key='journal'", (str(record["sequence"]),))

    def _purge_dependents(self, tenant: str, observation: str) -> None:
        from memoryking.adapters.report_store import Reports

        super()._purge_dependents(tenant, observation)
        Reports(self).purge(tenant)
