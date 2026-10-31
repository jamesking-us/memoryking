"""Bounded Unix-socket transport. No HTTP, browser cookies or claimed role authority."""

from __future__ import annotations

import os
import socket
import socketserver
import stat
import threading
from pathlib import Path
from typing import Any, cast

from memoryking.adapters.embedding_store import Embeddings
from memoryking.adapters.experience import Experience
from memoryking.adapters.handoff import Handoff
from memoryking.adapters.inference import Inference
from memoryking.adapters.knowledge_store import Knowledge
from memoryking.adapters.local_contract import VALIDATOR
from memoryking.adapters.local_store import LocalStore
from memoryking.adapters.processing_store import Processing
from memoryking.adapters.repository_store import Repository
from memoryking.adapters.retrieval import Retrieval
from memoryking.adapters.working import Working
from memoryking.application.local_port import LocalOperations
from memoryking.contracts.canonical import ContractError, canonical_bytes, load_strict
from memoryking.domain.local import LocalError, Released
from memoryking.observability import failure

WIRE_LIMIT = 262144


class StoreOperations:
    def __init__(self, store: LocalStore):
        self.store = store

    def execute(self, request: dict[str, Any]) -> dict[str, Any] | Released:
        if not VALIDATOR.is_valid(request):
            raise LocalError("invalid_request")
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
            store.reindex(credential)
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
            self.request.sendall(canonical_bytes({"error": code}) + b"\n")
        except (TimeoutError, BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            # Never serialize SQL, filenames, caller content or exception strings. The
            # request id links the generic reply to the redacted operator log line.
            request_id = failure("local_ipc", exc)
            self.request.sendall(
                canonical_bytes({"error": "internal_error", "request_id": request_id}) + b"\n"
            )


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Bounded concurrent requests: a slow client never blocks everyone else."""

    request_queue_size = 16
    daemon_threads = True
    block_on_close = True
    max_concurrent = 8

    def __init__(self, store: LocalStore):
        self.slots = threading.BoundedSemaphore(self.max_concurrent)
        self.path = store.root / "service.sock"
        if len(os.fsencode(self.path)) > 100:
            raise LocalError("socket_path_too_long")
        if self.path.exists():
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise LocalError("unsafe_socket")
            self.path.unlink()
        self.operations: LocalOperations = StoreOperations(store)
        self.store = store
        super().__init__(str(self.path), Handler)
        os.chmod(self.path, 0o600)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.slots.acquire(timeout=5):
            try:
                request.sendall(b'{"error":"service_busy"}\n')
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def service_actions(self) -> None:
        """Between requests, complete trivial audit jobs so the queue never fills."""
        try:
            self.store.drain()
            self.store.housekeeping()
        except LocalError:
            pass  # A fenced or unhealthy store reports its state on the next request.

    def server_close(self) -> None:
        super().server_close()
        self.path.unlink(missing_ok=True)


def request(
    socket_path: Path, credential: str, operation: str, args: dict[str, Any]
) -> dict[str, Any]:
    payload = canonical_bytes(
        {"version": 1, "credential": credential, "operation": operation, "args": args}
    )
    if len(payload) + 1 > WIRE_LIMIT:
        raise LocalError("invalid_request")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(10)
        stream.connect(str(socket_path))
        stream.sendall(payload + b"\n")
        with stream.makefile("rb") as response:
            raw = response.readline(1048577)
        value = load_strict(raw)
        if not isinstance(value, dict):
            raise LocalError("invalid_response")
        if "error" in value:
            raise LocalError(value["error"])
        return value
