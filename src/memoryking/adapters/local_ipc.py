"""Bounded Unix-socket transport. No HTTP, browser cookies or claimed role authority."""

from __future__ import annotations

import os
import socketserver
import stat
import threading
import time
from typing import Any, cast

from memoryking.adapters import local_client
from memoryking.adapters.embedding_store import Embeddings
from memoryking.adapters.experience import Experience
from memoryking.adapters.handoff import Handoff
from memoryking.adapters.inference import Inference
from memoryking.adapters.inspection import Inspection
from memoryking.adapters.knowledge_store import Knowledge
from memoryking.adapters.local_client import REPLY_DETAIL, WIRE_LIMIT
from memoryking.adapters.local_contract import OPERATIONS, VALIDATOR
from memoryking.adapters.platform_support import (
    UnixStreamServer,
    owned_by_current_user,
)
from memoryking.adapters.processing_store import Processing
from memoryking.adapters.repository_store import Repository
from memoryking.adapters.retrieval import Retrieval
from memoryking.adapters.user_scope import UserScope
from memoryking.adapters.working import Working
from memoryking.application.local_port import LocalOperations
from memoryking.application.store_core import StoreCore
from memoryking.contracts.canonical import ContractError, canonical_bytes, load_strict
from memoryking.contracts.problems import explain
from memoryking.domain.hints import hint
from memoryking.domain.local import LocalError, Released
from memoryking.observability import failure, summarize, timings, trace, tracing


def refusal(exc: LocalError) -> bytes:
    """The wire reply for a refusal: code, hint and any public detail."""
    return canonical_bytes(
        {"error": exc.code, "hint": hint(exc.code)}
        | {k: v for k, v in exc.detail.items() if k in REPLY_DETAIL}
    )


class StoreOperations:
    def __init__(self, store: StoreCore):
        self.store = store

    def execute(self, request: dict[str, Any]) -> dict[str, Any] | Released:
        """Dispatch and record the operation's latency and outcome for `/metrics`.

        With `serve --trace`, one redacted line per request also goes to the log:
        operation, command action, outcome, elapsed time, lock wait, time inside
        transactions and response size. Never the body, the project or the principal.
        """
        operation = request.get("operation") if isinstance(request, dict) else None
        # Only a known operation name labels metrics and trace lines; caller text never does.
        name = operation if isinstance(operation, str) and operation in OPERATIONS else "invalid"
        started = time.monotonic()
        error: str | None = None
        spans = timings(self.store) if tracing() else None
        response: dict[str, Any] | Released | None = None
        try:
            response = self._execute(request)
            return response
        except LocalError as exc:
            error = exc.code
            raise
        except Exception:
            error = "internal_error"
            raise
        finally:
            elapsed = (time.monotonic() - started) * 1000
            self.store.counters.observe(name, elapsed, error)
            if spans is not None:
                self.store.timings.items = None
                trace(
                    "operations",
                    operation=name,
                    action=self._action(request),
                    outcome=error or "ok",
                    response_bytes=len(response.body)
                    if isinstance(response, Released)
                    else len(canonical_bytes(response))
                    if response is not None
                    else 0,
                    **summarize(elapsed, spans),
                )

    @staticmethod
    def _action(request: Any) -> str:
        """The command action (a fixed vocabulary such as brief), never free text."""
        try:
            action = request["args"]["command"]["action"]
        except (KeyError, TypeError):
            return "-"
        return action if isinstance(action, str) and action.isidentifier() else "-"

    def _execute(self, request: dict[str, Any]) -> dict[str, Any] | Released:
        found = explain(VALIDATOR, request)
        if found is not None:
            raise LocalError("invalid_request", detail=found)
        op, args, credential = request["operation"], request["args"], request["credential"]
        store = self.store
        if op == "experience":
            return Experience(store).execute(credential, args["command"])
        if op == "processing":
            return Processing(store).execute(credential, args["command"])
        if op == "embedding":
            return Embeddings(store).execute(credential, args["command"])
        if op == "repository":
            return Repository(store).execute(credential, args["command"])
        if op == "inference":
            return Inference(store).execute(credential, args["command"])
        if op == "handoff":
            return Handoff(store).execute(credential, args["command"])
        if op == "context":
            return store.context(credential, args["project"])
        if op == "working":
            return Working(store).execute(credential, args["command"])
        if op == "retrieval":
            return Retrieval(store).execute(credential, args["command"])
        if op == "knowledge":
            return Knowledge(store).execute(credential, args["command"])
        if op == "inspection":
            return Inspection(store).execute(credential, args["command"])
        if op == "dashboard_bootstrap":
            dashboard = getattr(store, "dashboard", None)
            if dashboard is None:
                raise LocalError("dashboard_disabled")
            return cast(dict[str, Any], dashboard.issue(credential))
        if op == "namespace":
            return {"namespace": store.namespace(credential, **args)}
        if op == "capture":
            return store.capture(credential, **args)
        if op in {"read", "export"}:
            return store.read(credential, **args, export=op == "export")
        if op == "status":
            return store.status(credential, **args)
        if op == "grants":
            return {"items": store.grants(credential)}
        if op == "user":
            return UserScope(store).execute(credential, args)
        if op == "claim":
            return {"job": store.claim(credential, **args)}
        if op == "finish":
            store.finish(credential, **args)
        elif op == "grant":
            return {"credential": store.grant(credential, **args)}
        elif op == "revoke":
            store.revoke(credential, **args)
        elif op == "purge":
            store.purge(credential, **args)
        elif op == "reindex":
            return {"ok": True, "lexical": store.reindex(credential)}
        return {"ok": True}


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(5)
        try:
            raw = self.rfile.readline(WIRE_LIMIT + 1)
            if len(raw) > WIRE_LIMIT or not raw.endswith(b"\n"):
                raise LocalError("invalid_request")
            value = load_strict(raw)
            if not isinstance(value, dict):
                raise LocalError("invalid_request")
            operations: LocalOperations = self.server.operations  # type: ignore[attr-defined]
            response = operations.execute(value)
            if isinstance(response, Released):
                response.emit(lambda data: self.request.sendall(data + b"\n"))
            else:
                self.request.sendall(canonical_bytes(response) + b"\n")
        except (LocalError, ContractError) as exc:
            code = exc.code if isinstance(exc, LocalError) else "invalid_request"
            self.request.sendall(
                (refusal(exc) if isinstance(exc, LocalError) else refusal(LocalError(code))) + b"\n"
            )
        except (TimeoutError, BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            # Never serialize SQL, filenames, caller content or exception strings. The
            # request id links the generic reply to the redacted operator log line.
            request_id = failure("local_ipc", exc)
            self.request.sendall(
                canonical_bytes({"error": "internal_error", "request_id": request_id}) + b"\n"
            )


class Server(socketserver.ThreadingMixIn, UnixStreamServer):
    """Bounded concurrent requests: a slow client never blocks everyone else."""

    request_queue_size = 16
    daemon_threads = True
    block_on_close = True
    max_concurrent = 8
    max_waiting = 16
    wait_seconds = 5

    def __init__(self, store: StoreCore, *, workers: int | None = None, waiters: int | None = None):
        """`workers` requests run at once and up to `waiters` more wait for a worker (defaults 8
        and 16); a connection beyond both is refused as `service_busy`."""
        if workers is not None:
            if not 1 <= workers <= 256:
                raise LocalError("invalid_concurrency")
            self.max_concurrent = workers
        if waiters is not None:
            if not 0 <= waiters <= 1024:
                raise LocalError("invalid_concurrency")
            self.max_waiting = waiters
        self.slots = threading.BoundedSemaphore(self.max_concurrent)
        self.waiting = threading.BoundedSemaphore(max(1, self.max_waiting))
        if self.max_waiting == 0:
            self.waiting.acquire()  # no waiting room at all
        self.path = store.root / "service.sock"
        if len(os.fsencode(self.path)) > 100:
            raise LocalError("socket_path_too_long")
        if self.path.exists():
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or not owned_by_current_user(info.st_uid):
                raise LocalError("unsafe_socket")
            self.path.unlink()
        self.operations: LocalOperations = StoreOperations(store)
        self.store = store
        store.concurrency_gauge = self.saturation
        super().__init__(str(self.path), Handler)
        os.chmod(self.path, 0o600)

    def process_request(self, request: Any, client_address: Any) -> None:
        # This runs on the accept thread, which must never wait: a connection that finds all
        # workers busy waits on its own thread, up to `max_waiting` at once, and any beyond
        # that bound is refused immediately so the daemon never holds unbounded sockets.
        if self.slots.acquire(blocking=False):
            self._serve(request, client_address)
        elif self.waiting.acquire(blocking=False):
            try:
                threading.Thread(
                    target=self._wait_then_serve, args=(request, client_address), daemon=True
                ).start()
            except BaseException:
                self.waiting.release()
                self._refuse(request)
                raise
        else:
            self._refuse(request)

    def _serve(self, request: Any, client_address: Any) -> None:
        """Hand a connection that already holds a slot to a worker; the slot is released after."""
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def _wait_then_serve(self, request: Any, client_address: Any) -> None:
        try:
            got = self.slots.acquire(timeout=self.wait_seconds)
        finally:
            self.waiting.release()
        if got:
            self._serve(request, client_address)
        else:
            self._refuse(request)

    def _refuse(self, request: Any) -> None:
        try:
            request.sendall(b'{"error":"service_busy"}\n')
        except OSError:
            pass
        self.shutdown_request(request)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def saturation(self) -> dict[str, tuple[int, int]]:
        """(limit, in use) for the worker slots and the waiting room."""
        free = getattr(self.slots, "_value", self.max_concurrent)
        room = getattr(self.waiting, "_value", 0)
        waiting = 0 if self.max_waiting == 0 else self.max_waiting - room
        return {
            "local_workers": (self.max_concurrent, self.max_concurrent - free),
            "local_waiters": (self.max_waiting, waiting),
        }

    def service_actions(self) -> None:
        """Called by the accept loop between polls. It does nothing: draining, housekeeping and
        scheduled backups run on the maintenance thread, because a backup or a long expiry on
        the accept thread would stop the daemon accepting connections for as long as it ran."""

    def maintain(self) -> None:
        """One maintenance pass: complete trivial audit jobs so the queue never fills, then
        housekeeping (expiry, retention, the blob layout move and scheduled backups).

        Draining and housekeeping are independent: a failure in one never skips the other
        or ends the thread. A fenced or unhealthy store reports its state on the next
        request; anything else is logged."""
        for action in (self.store.drain, self.store.housekeeping, self.store.flush_anchor):
            try:
                action()
            except LocalError:
                pass
            except Exception as exc:
                failure("local_ipc", exc, action=action.__name__)

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        stop = threading.Event()

        def loop() -> None:
            while not stop.wait(poll_interval):
                self.maintain()

        thread = threading.Thread(target=loop, name="maintenance", daemon=True)
        thread.start()
        try:
            super().serve_forever(poll_interval)
        finally:
            stop.set()
            thread.join(60)

    def server_close(self) -> None:
        super().server_close()
        self.path.unlink(missing_ok=True)


# The client end lives in local_client so a client need not import the store; kept here
# for callers that import it from this module.
request = local_client.request
