"""Authenticated shared request boundary; domain operations remain transport-neutral."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, NoReturn

from memoryking.adapters import blobs as blobfiles
from memoryking.adapters.local_contract import VALIDATOR
from memoryking.adapters.local_ipc import StoreOperations
from memoryking.adapters.postgres_store import PostgresStore
from memoryking.adapters.shared_control import SharedControl
from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import LocalError, Released
from memoryking.health import CHECKS
from memoryking.observability import event, failure
from memoryking.observability_metrics import Gauge, Metrics, render


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
        # Retention runs on a thread of its own (`start_housekeeping`), never inside a request.
        # `_housekeeping` is the wall time of each tenant's last successful pass.
        self._housekeeping: dict[str, float] = {}
        self._housekeeping_lock = threading.Lock()
        self._layout_done: set[str] = set()
        self.housekeeping_failures = 0
        self.http_statuses: dict[int, int] = {}
        self.housekeeping_passes = 0
        self.stopping = threading.Event()
        self.metrics = Metrics()

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

    def readiness(self) -> dict[str, bool]:
        """The three checks behind `/readyz`: the database answers, the control tree is
        readable, and the database's schema version is one this build accepts."""
        from memoryking.adapters.postgres_admin import schema_compatible
        from memoryking.adapters.postgres_pool import acquire

        checks = dict.fromkeys(CHECKS, False)
        try:
            connection = acquire(self.pool, timeout=2)
            try:
                row = connection.execute(
                    "SELECT value FROM memoryking.shared_meta WHERE key='schema'"
                ).fetchone()
                connection.rollback()
            finally:
                self.pool.putconn(connection)
            checks["database"] = True
            checks["schema"] = bool(row and row[0].isdigit() and schema_compatible(int(row[0])))
        except Exception:
            pass
        checks["control"] = self.control.is_dir() and os.access(self.control, os.R_OK | os.X_OK)
        return checks

    def pool_stats(self) -> dict[str, int]:
        try:
            stats = self.pool.get_stats()
        except Exception:
            return {}
        return {
            "size": int(stats.get("pool_size", 0)),
            "available": int(stats.get("pool_available", 0)),
            "waiting": int(stats.get("requests_waiting", 0)),
            "max": int(stats.get("pool_max", 0)),
        }

    def metrics_text(
        self, tenant: str, credential: str, deadline: float | None, ready: dict[str, bool]
    ) -> str:
        """Prometheus text for one tenant, behind that tenant's admin credential.

        Process-wide counters (requests, errors by code, latency, gate wait) carry no tenant
        or caller identity; the gauges (capacity bounds, journal size, backup age) are the
        authenticating tenant's own. Everything is a count, a size or a time, under fixed names."""
        with self.store(tenant, credential, release_only=True, request_deadline=deadline) as store:
            status = store.status(credential, "metrics")
        directory = self.control / tenant
        now = time.time()
        extra: list[Gauge] = []
        try:
            journal = (directory / "journal").stat().st_size
        except OSError:
            journal = 0
        extra.append(("memoryking_journal_bytes", "Size of the control journal.", [({}, journal)]))
        try:
            last = int((directory / "last-backup").read_text().strip()) / 1000
            age = max(0.0, now - last)
        except (OSError, ValueError):
            age = -1.0  # no backup has completed
        extra.append(
            (
                "memoryking_backup_age_seconds",
                "Seconds since the last completed backup; -1 when there is none.",
                [({}, age)],
            )
        )
        with self._housekeeping_lock:
            done = self._housekeeping.get(tenant)
            failures, passes = self.housekeeping_failures, self.housekeeping_passes
        extra.append(
            (
                "memoryking_housekeeping_age_seconds",
                "Seconds since housekeeping last finished for this tenant; -1 when it has not.",
                [({}, -1.0 if done is None else max(0.0, now - done))],
            )
        )
        extra.append(
            (
                "memoryking_housekeeping_failures_total",
                "Housekeeping failures since start.",
                [({}, failures)],
            )
        )
        extra.append(
            (
                "memoryking_housekeeping_passes_total",
                "Housekeeping passes since start.",
                [({}, passes)],
            )
        )
        extra.append(
            (
                "memoryking_ready",
                "1 when a readiness check passes.",
                [({"check": name}, float(ready.get(name, False))) for name in CHECKS],
            )
        )
        with self._housekeeping_lock:
            statuses = dict(self.http_statuses)
        extra.append(
            (
                "memoryking_http_responses_total",
                "HTTP responses by status code.",
                [({"status": str(code)}, count) for code, count in sorted(statuses.items())],
            )
        )
        pool = self.pool_stats()
        extra.append(
            (
                "memoryking_pool",
                "Database pool: connections open, idle, requests waiting and the maximum.",
                [({"state": key}, value) for key, value in sorted(pool.items())],
            )
        )
        extra.append(
            (
                "memoryking_concurrency",
                "Configured limit and current use of each bounded concurrency resource.",
                [
                    ({"resource": name, "kind": kind}, value)
                    for name, (limit, used) in sorted(self.concurrency().items())
                    for kind, value in (("limit", limit), ("in_use", used))
                ],
            )
        )
        return render(self.metrics, status, extra)

    def http_response(self, status: int) -> None:
        with self._housekeeping_lock:
            self.http_statuses[status] = self.http_statuses.get(status, 0) + 1

    # name -> (limit, in use); the HTTP boundary fills this in when it starts.
    http_gauge: Any = None

    def concurrency(self) -> dict[str, tuple[int, int]]:
        pool = self.pool_stats()
        found = {
            "database_pool": (pool.get("max", 0), pool.get("size", 0) - pool.get("available", 0))
        }
        if self.http_gauge is not None:
            found["http_requests"] = self.http_gauge()
        return found

    HOUSEKEEPING_S = 3600
    SWEEP_EVERY = 24  # passes between full blob sweeps (one a day at the default interval)

    def tenants(self) -> list[str]:
        try:
            return sorted(
                path.name for path in self.control.iterdir() if (path / "identity.json").is_file()
            )
        except OSError:
            return []

    def maintain(self, tenant: str) -> None:
        """Expire results, receipts, usage, egress audit and summaries, release old rows and
        finish an unfinished lexical rebuild, for one tenant, as the service itself.

        The service signs a credential for the reserved principal `housekeeping` with the
        tenant's own control key: it carries no grant, authorizes nothing, and only lets the
        store open. It does not read any tenant content."""
        control = SharedControl(self.control / tenant)
        credential = control.sign(
            {
                "kind": "credential",
                "store": control.store,
                "incarnation": control.read_anchor()["incarnation"],
                "tenant": tenant,
                "principal": "housekeeping",
                "audience": self.audience,
            }
        )
        # A pool saturated by requests must not starve this pass for good: it waits a bounded
        # time for a connection and the next pass tries again.
        with self.store(
            tenant, credential, request_deadline=time.monotonic() + self.CHECKOUT_S
        ) as store:
            store.expire_results()
            store.rebuild_lexical(wall_ms=2000)
            if tenant not in self._layout_done:
                # Flat blob files move into their shard directories, under the exclusive gate
                # so no request of this tenant reads a file while it is renamed.
                with store.transaction():
                    if blobfiles.migrate(self.root, budget_s=1.0, tenant=tenant):
                        self._layout_done.add(tenant)
            if self.housekeeping_passes % self.SWEEP_EVERY == 0:
                # The explicit orphan sweep, off the request path: purge deletes its own file,
                # so this only finds what a crash left behind.
                store.gc()

    CHECKOUT_S = 30.0

    def housekeeping_pass(self) -> int:
        """One pass over every tenant; a failure in one is logged and never skips the others.
        Returns the number of tenants that failed."""
        failed = 0
        for tenant in self.tenants():
            started = time.monotonic()
            try:
                self.maintain(tenant)
            except LocalError as exc:
                failed += 1
                event("shared_service", "housekeeping_refused", code=exc.code)
            except Exception as exc:
                failed += 1
                failure("shared_service", exc, action="housekeeping")
            else:
                with self._housekeeping_lock:
                    self._housekeeping[tenant] = time.time()
            self.metrics.observe("housekeeping", (time.monotonic() - started) * 1000, None)
        with self._housekeeping_lock:
            self.housekeeping_failures += failed
            self.housekeeping_passes += 1
        return failed

    def start_housekeeping(self, interval_s: float | None = None) -> threading.Thread:
        """A thread of the service's own, so retention never waits for a request: the first
        pass runs right after start, then once per interval."""
        wait = float(self.HOUSEKEEPING_S if interval_s is None else interval_s)

        def run() -> None:
            while not self.stopping.is_set():
                try:
                    self.housekeeping_pass()
                except Exception as exc:  # the loop itself must not die
                    failure("shared_service", exc, action="housekeeping_loop")
                self.stopping.wait(wait)

        thread = threading.Thread(target=run, name="housekeeping", daemon=True)
        thread.start()
        return thread

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
        # One set of counters for the process: a store is built per request.
        store.counters = self.metrics
        return store

    def reject(self, code: str) -> NoReturn:
        """A request refused before any store handled it is still counted, under the name
        `rejected` (never the caller's text), so error counts by code cover it."""
        self.metrics.observe("rejected", 0.0, code)
        raise LocalError(code)

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
            self.reject("invalid_request")
        # No body credential may replace the authenticated bearer identity.
        value = {**request, "credential": credential}
        if request["operation"] == "delegate":
            args = request["args"]
            if (
                request["version"] != 1
                or not isinstance(args, dict)
                or set(args) != {"principal", "project", "capabilities", "ttl_ms"}
            ):
                self.reject("invalid_request")
            if not isinstance(args["capabilities"], list) or not all(
                isinstance(v, str) for v in args["capabilities"]
            ):
                self.reject("invalid_request")
            with self.store(tenant, credential, request_deadline=deadline) as store:
                return {"credential": store.delegate(**args)}
        if not VALIDATOR.is_valid(value):
            self.reject("invalid_request")
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
            try:
                outcome = StoreOperations(store).execute(value)
            finally:
                for span in store.metrics:
                    if "gate_wait_ms" in span:
                        self.metrics.observe("gate_wait", span["gate_wait_ms"], None)
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
