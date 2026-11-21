"""Authenticated shared request boundary; domain operations remain transport-neutral."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from memoryking.adapters.local_contract import VALIDATOR
from memoryking.adapters.local_ipc import StoreOperations
from memoryking.adapters.postgres_store import PostgresStore
from memoryking.adapters.shared_control import SharedControl
from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import LocalError, Released
from memoryking.observability import failure


class SharedService:
    def __init__(
        self,
        pool: Any,
        root: Path,
        control: Path,
        *,
        audience: str = "memoryking-shared",
        instance_id: str | None = None,
    ):
        self.pool, self.root, self.control, self.audience = pool, root, control, audience
        self.instance_id = instance_id
        self.dashboard: Any = None
        # Retention runs per tenant at most hourly from request traffic: a store is
        # built per request, so the timer lives here, not on the store.
        self._housekeeping: dict[str, float] = {}
        self._housekeeping_lock = threading.Lock()

    egress_policy: Any | None = None

    def warm(self) -> None:
        """Verify every tenant's control journal once, so no request pays for a cold start.

        Runs in the background after start. It only fills the same verified-journal cache a
        request would, under the same control lock and against the same anchor; a tenant
        whose journal does not verify is reported without a reason beyond the error type and
        stays refused by the request path.
        """
        try:
            directories = sorted(
                path for path in self.control.iterdir() if (path / "identity.json").is_file()
            )
        except OSError:
            return
        for directory in directories:
            try:
                control = SharedControl(directory)
                with control.locked():
                    control.verified_count(control.read_anchor())
            except Exception as exc:
                failure("shared_service", exc, action="warm")

    HOUSEKEEPING_S = 3600
    REBUILD_S = 5  # how often a request may advance an unfinished lexical rebuild

    def _rebuild(self, tenant: str, store: PostgresStore) -> None:
        """Advance an unfinished lexical rebuild in bounded batches, between requests."""
        now = time.monotonic()
        key = tenant + "\0rebuild"
        with self._housekeeping_lock:
            if now - self._housekeeping.get(key, -self.REBUILD_S) < self.REBUILD_S:
                return
            self._housekeeping[key] = now
        try:
            store.rebuild_lexical(wall_ms=1000)
        except LocalError:
            pass
        except Exception as exc:
            failure("shared_service", exc, action="rebuild")

    def _retention(self, tenant: str, store: PostgresStore) -> None:
        """Expire results, receipts, usage, egress audit and summaries for this tenant."""
        now = time.monotonic()
        with self._housekeeping_lock:
            if now - self._housekeeping.get(tenant, -self.HOUSEKEEPING_S) < self.HOUSEKEEPING_S:
                return
            self._housekeeping[tenant] = now
        try:
            store.expire_results()
            store.rebuild_lexical(wall_ms=2000)  # finishes an unfinished lexical rebuild in batches
        except LocalError:
            pass
        except Exception as exc:
            failure("shared_service", exc, action="retention")

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
            instance_id=self.instance_id,
        )
        store.egress_policy = self.egress_policy
        return store

    def execute(
        self,
        tenant: str,
        credential: str,
        request: dict[str, Any],
        deadline: float | None = None,
    ) -> dict[str, Any] | Released:
        """`deadline` is the request's monotonic budget; checkout and statements stop at it."""
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
            with self.store(tenant, credential, request_deadline=deadline) as store:
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
        with self.store(
            tenant, credential, release_only=release_only, request_deadline=deadline
        ) as store:
            outcome = StoreOperations(store).execute(value)
            if not release_only:
                self._retention(tenant, store)
                self._rebuild(tenant, store)
            if request["operation"] == "capture":
                # A tenant that captures also drains its own audit jobs, so the shared
                # profile never depends solely on the scheduled worker to stay under
                # the queue limit.
                try:
                    store.drain(limit=64)
                except LocalError:
                    pass
                except Exception as exc:
                    # The capture already committed: never turn it into an error reply.
                    failure("shared_service", exc, action="drain")
            return outcome
