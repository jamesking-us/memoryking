"""Authenticated shared request boundary; domain operations remain transport-neutral."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from memoryking.adapters.local_contract import VALIDATOR
from memoryking.adapters.local_ipc import StoreOperations
from memoryking.adapters.postgres_store import PostgresStore
from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import LocalError, Released


class SharedService:
    def __init__(
        self, pool: Any, root: Path, control: Path, *, audience: str = "memoryking-shared"
    ):
        self.pool, self.root, self.control, self.audience = pool, root, control, audience
        self.dashboard: Any = None

    def store(self, tenant: str, credential: str, *, release_only: bool = False) -> PostgresStore:
        return PostgresStore(
            self.pool,
            self.root,
            self.control,
            tenant,
            credential,
            audience=self.audience,
            release_only=release_only,
        )

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
            outcome = StoreOperations(store).execute(value)
            if request["operation"] == "capture":
                # A tenant that captures also drains its own audit jobs, so the shared
                # profile never depends solely on the scheduled worker to stay under
                # the queue limit.
                try:
                    store.drain(limit=64)
                except LocalError:
                    pass
            return outcome
