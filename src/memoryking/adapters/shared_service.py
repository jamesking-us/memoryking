"""Authenticated shared request boundary; domain operations remain transport-neutral."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from memoryking.adapters.local_contract import VALIDATOR
from memoryking.adapters.local_ipc import StoreOperations
from memoryking.adapters.postgres_store import PostgresStore
from memoryking.adapters.report_worker import Configuration
from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import LocalError, Released


class SharedService:
    def __init__(
        self, pool: Any, root: Path, control: Path, *, audience: str = "memoryking-shared"
    ):
        self.pool, self.root, self.control, self.audience = pool, root, control, audience
        self.dashboard: Any = None
        self.report_configuration: Configuration | None = None

    def store(
        self,
        tenant: str,
        credential: str,
        *,
        release_only: bool = False,
        request_deadline: float | None = None,
    ) -> PostgresStore:
        store = PostgresStore(
            self.pool,
            self.root,
            self.control,
            tenant,
            credential,
            audience=self.audience,
            release_only=release_only,
            request_deadline=request_deadline,
        )
        config = self.report_configuration
        if config is not None and config.enabled and tenant == config.tenant:
            store.report_mail = config.mail  # type: ignore[attr-defined]
        return store

    def viewer_auth_store(self, tenant: str, deadline: float) -> PostgresStore:
        """Internal identity-query connection; never a bearer/owner credential or route.

        No grants are created. Only the closed auth adapter receives this factory.
        Current incarnation is read for every checkout so restore cannot reuse a login.
        """
        from memoryking.adapters.local_store import identifier
        from memoryking.adapters.shared_control import SharedControl

        identifier(tenant)
        control = SharedControl(self.control / tenant)
        anchor = control.read_anchor()
        token = control.sign(
            {
                "kind": "credential",
                "store": control.store,
                "incarnation": anchor["incarnation"],
                "tenant": tenant,
                "principal": "viewer-auth-internal",
                "audience": self.audience,
            }
        )
        return self.store(tenant, token, request_deadline=deadline)

    def execute(
        self, tenant: str, credential: str, request: dict[str, Any]
    ) -> dict[str, Any] | Released:
        if (
            not isinstance(request, dict)
            or set(request) != {"version", "operation", "args"}
            or len(canonical_bytes(request)) > 262144
        ):
            raise LocalError("invalid_request")
        # No body credential may replace the authenticated bearer identity.
        value = {**request, "credential": credential}
        if request["operation"] == "delegate":
            args = request["args"]
            if (
                request["version"] != 1
                or not isinstance(args, dict)
                or set(args) != {"principal", "project", "capabilities", "ttl_ms"}
            ):
                raise LocalError("invalid_request")
            if not isinstance(args["capabilities"], list) or not all(
                isinstance(v, str) for v in args["capabilities"]
            ):
                raise LocalError("invalid_request")
            with self.store(tenant, credential) as store:
                return {"credential": store.delegate(**args)}
        if not VALIDATOR.is_valid(value):
            raise LocalError("invalid_request")
        if request["operation"] == "dashboard_bootstrap":
            if self.dashboard is None:
                raise LocalError("dashboard_disabled")
            result: dict[str, Any] = self.dashboard.issue(tenant, credential)
            return result
        # Domain writes and stateful permit/lease/CAS operations stay exclusive.
        release_only = request["operation"] in {"read", "export", "context", "status", "namespace"}
        with self.store(tenant, credential, release_only=release_only) as store:
            return StoreOperations(store).execute(value)
