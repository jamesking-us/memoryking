"""Durable host-driven reference task harness with a readback-capable local file tool.

No model output selects tools or grants authority. Every mutation is an explicit host
call; the only provider is the local recorder. The journal is private recovery state.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from memoryking.adapters.local_files import Ownership, atomic, private_dir, private_file, sync_dir
from memoryking.contracts.canonical import canonical_bytes, canonical_hash, load_strict
from memoryking.contracts.validation import validate_working
from memoryking.domain.local import LocalError
from memoryking.harness_context import AuthorizedConversation, LocalRecordingProvider
from memoryking.providers import HttpAdapter
from memoryking.sdk import BINDING_FIELDS, LocalClient


class LocalFileTool:
    """Create one bounded private file exclusively; readback never repeats the effect."""

    def __init__(self, root: Path):
        private_dir(root, create=True)
        self.root = root.resolve()

    def target(self, name: str) -> Path:
        private_dir(self.root)
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", name) is None or name in {".", ".."}:
            raise LocalError("tool_path_invalid")
        return self.root / name

    def payload(self, operation: str, content: str) -> bytes:
        if len(content.encode()) > 16384:
            raise LocalError("tool_input_limit")
        return canonical_bytes({"operation": operation, "content": content})

    def create(self, name: str, data: bytes) -> None:
        target = self.target(name)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError as exc:
            raise LocalError("tool_target_exists") from exc
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        sync_dir(self.root)

    def inspect(self, name: str, expected_hash: str) -> dict[str, Any]:
        path = self.target(name)
        if not path.exists() and not path.is_symlink():
            return {"state": "failed", "reason": "absent_at_readback", "path": name, "hash": None}
        try:
            private_file(path)
            with path.open("rb") as stream:
                raw = stream.read(32769)
            hashed = hashlib.sha256(raw).hexdigest()
            if len(raw) > 32768 or hashed != expected_hash:
                raise LocalError("tool_readback_mismatch")
            return {"state": "succeeded", "reason": "exact_readback", "path": name, "hash": hashed}
        except (LocalError, OSError):
            return {
                "state": "unknown",
                "reason": "readback_unavailable_or_mismatched",
                "path": name,
                "hash": None,
            }


class ReferenceHarness:
    """All accepted changes are journaled; each boundary persists a full checkpoint.

    Staged state is flushed within flush_seconds (at most 60) while the harness runs.
    Capture failure prevents later mutations. Resumption never redispatches a tool.
    """

    def __init__(
        self,
        client: LocalClient,
        directory: Path,
        tool: LocalFileTool,
        *,
        task: str,
        run: str | None = None,
        state: dict[str, Any] | None = None,
        readers: list[str] | None = None,
        bindings: list[dict[str, Any]] | None = None,
        flush_seconds: float = 30,
        fault: Callable[[str], None] | None = None,
    ):
        if type(tool) is not LocalFileTool or not 0.05 <= flush_seconds <= 60:
            raise LocalError("harness_configuration_invalid")
        private_dir(directory, create=True)
        self.client, self.directory, self.tool = client, directory, tool
        self.path = directory / "task.json"
        self.lock = threading.RLock()
        self.owner = Ownership(directory)
        self.stop = threading.Event()
        self.fault = fault or (lambda name: None)
        self.closed = False
        self.error: str | None = None
        self.flush_seconds = flush_seconds
        try:
            context = client.context()
            binding = {key: context[key] for key in BINDING_FIELDS}
            if self.path.exists():
                if state is not None:
                    raise LocalError("harness_already_exists")
                private_file(self.path)
                with self.path.open("rb") as stream:
                    raw = stream.read(262145)
                if len(raw) > 262144:
                    raise LocalError("harness_journal_limit")
                document = load_strict(raw)
                if (
                    not isinstance(document, dict)
                    or set(document) != {"format", "sha256", "payload"}
                    or document["format"] != "reference-harness-v1"
                    or canonical_hash(document["payload"]) != document["sha256"]
                ):
                    raise LocalError("harness_journal_integrity")
                self.data = document["payload"]
                required = {
                    "version",
                    "binding",
                    "task",
                    "run",
                    "readers",
                    "bindings",
                    "tool_root",
                    "state",
                    "revision",
                    "checkpoint",
                    "sequence",
                    "pending",
                    "operation",
                    "staged",
                    "activity",
                    "handoff_checkpoint",
                }
                if (
                    not isinstance(self.data, dict)
                    or set(self.data) != required
                    or self.data["version"] != 1
                ):
                    raise LocalError("harness_journal_invalid")
                if self.data["binding"] != binding or (
                    self.data["task"],
                    self.data["run"],
                    self.data["tool_root"],
                ) != (task, run, str(tool.root)):
                    raise LocalError("harness_binding_changed")
                self._validate(self.data["state"])
                self._flush_pending()
                if self.data["checkpoint"] is None:
                    self._record("task_started_recovered", {})
                self._guard(ready=False)
                self._recover_operation()
                if (
                    self.data["activity"] is not None
                    and self.data["activity"]["outcome"] == "pending"
                ):
                    state = self.state()
                    state["findings"].append(
                        {
                            "kind": "hypothesis",
                            "text": "Interrupted model turn: its unobserved result was not restored or reused.",
                            "evidence": [],
                        }
                    )
                    self._record(
                        "model_interrupted",
                        {"activity": self.data["activity"], "result": "unobserved"},
                        state,
                    )
                self.data["activity"] = None
                self._persist()
                if self.data["staged"] is not None:
                    self._record("recovered_staged_state", {})
            else:
                if state is None:
                    raise LocalError("harness_state_required")
                self.data = {
                    "version": 1,
                    "binding": binding,
                    "task": task,
                    "run": run,
                    "readers": readers if readers is not None else [binding["principal"]],
                    "bindings": bindings if bindings is not None else [{}],
                    "tool_root": str(tool.root),
                    "state": copy.deepcopy(state),
                    "revision": 0,
                    "checkpoint": None,
                    "sequence": 0,
                    "pending": None,
                    "operation": None,
                    "staged": None,
                    "activity": None,
                    "handoff_checkpoint": None,
                }
                self._validate(state)
                self._persist()
                self._record("task_started", {})
        except BaseException:
            self.owner.close()
            self.closed = True
            raise
        self.worker = threading.Thread(
            target=self._periodic, name="memoryking-capture", daemon=True
        )
        self.worker.start()

    def _scope(self) -> dict[str, Any]:
        return {"project": self.client.project, "task": self.data["task"], "run": self.data["run"]}

    def _command(self, action: str, args: dict[str, Any], key: str | None = None) -> dict[str, Any]:
        return {
            "version": 1,
            "incarnation": self.data["binding"]["incarnation"],
            "action": action,
            "args": args,
            **({"key": key} if key is not None else {}),
        }

    def _validate(self, state: dict[str, Any]) -> None:
        validate_working(
            self._command(
                "save",
                {
                    **self._scope(),
                    "expected_revision": self.data["revision"],
                    "readers": self.data["readers"],
                    "checkpoint": state,
                },
                "journal-validation",
            )
        )

    def _persist(self) -> None:
        raw = canonical_bytes(
            {
                "format": "reference-harness-v1",
                "sha256": canonical_hash(self.data),
                "payload": self.data,
            }
        )
        if len(raw) > 262144:
            raise LocalError("harness_journal_limit")
        atomic(self.path, raw)

    def _available(self) -> None:
        if self.closed:
            raise LocalError("harness_closed")
        self.owner.check()
        if self.error:
            raise LocalError("harness_capture_degraded")

    def _periodic(self) -> None:
        while not self.stop.wait(self.flush_seconds):
            with self.lock:
                if not self.error:
                    try:
                        if self.data["staged"] is not None:
                            self._record("periodic_state", {})
                        self.heartbeat()
                    except Exception as exc:
                        self.error = exc.code if isinstance(exc, LocalError) else "capture_failed"

    def heartbeat(self) -> dict[str, Any]:
        """An observed signal, not a checkpoint, progress increment or claim of completion."""
        with self.lock:
            self._available()
            event = "heartbeat_" + uuid.uuid4().hex
            observed = time.time_ns() // 1_000_000
            payload = {
                "version": 1,
                "id": event,
                **self._scope(),
                "checkpoint": self.data["checkpoint"],
                "revision": self.data["revision"],
                "activity": "model"
                if self.data["activity"]
                else "tool"
                if self.data["operation"]
                else "idle",
                "observed_ms": observed,
            }
            return self.client.capture(
                event,
                {
                    "version": 1,
                    "project": self.client.project,
                    "source": "reference-heartbeat-v1",
                    "source_event": event,
                    "text": canonical_bytes(payload).decode(),
                    "classification": "restricted",
                    "readers": self.data["readers"],
                    "observed_at_ms": observed,
                },
            )

    def _flush_pending(self) -> dict[str, Any] | None:
        pending = self.data["pending"]
        if pending is None:
            return None
        try:
            captured = self.client.capture(pending["id"], pending["observation"])
            body = copy.deepcopy(pending["state"])
            reference = {"kind": "observation", "id": captured["observation"]}
            body["references"].append(reference)
            body["capture"].update(
                last_event=pending["id"], observed_ms=pending["observation"]["observed_at_ms"]
            )
            result = self.client.working(
                self._command(
                    "save",
                    {
                        **self._scope(),
                        "expected_revision": pending["expected_revision"],
                        "readers": self.data["readers"],
                        "checkpoint": body,
                    },
                    pending["id"],
                )
            )
            self.fault("after_checkpoint_ack")
            self.data.update(
                state=body,
                revision=result["revision"],
                checkpoint=result["checkpoint"],
                pending=None,
                staged=None,
            )
            self._persist()
            self.error = None
            return reference
        except Exception as exc:
            self.error = exc.code if isinstance(exc, LocalError) else "capture_failed"
            raise

    def _record(
        self, kind: str, details: dict[str, Any], state: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self._flush_pending()
        body = copy.deepcopy(
            state if state is not None else (self.data["staged"] or self.data["state"])
        )
        self._validate(body)
        if len(body["references"]) >= 120:
            raise LocalError("harness_evidence_capacity")
        event = "event_" + uuid.uuid4().hex
        observed = time.time_ns() // 1_000_000
        self.data["sequence"] += 1
        payload = {
            "version": 1,
            "kind": kind,
            "id": event,
            "sequence": self.data["sequence"],
            **self._scope(),
            "observed_ms": observed,
            "previous_checkpoint": self.data["checkpoint"],
            "previous_revision": self.data["revision"],
            "details": details,
        }
        observation = {
            "version": 1,
            "project": self.client.project,
            "source": "reference-harness-v1",
            "source_event": event,
            "text": canonical_bytes(payload).decode(),
            "classification": "restricted",
            "readers": self.data["readers"],
            "observed_at_ms": observed,
        }
        self.data["pending"] = {
            "id": event,
            "observation": observation,
            "state": body,
            "expected_revision": self.data["revision"],
        }
        self._persist()
        self.fault("after_event_journal")
        result = self._flush_pending()
        assert result is not None
        return result

    def _guard(self, *, ready: bool = True) -> dict[str, Any]:
        self._available()
        self._flush_pending()
        if self.data["staged"] is not None:
            self._record("before_action_state", {})
        packet = self.client.working(
            self._command(
                "resume",
                {
                    **self._scope(),
                    "checkpoint": None,
                    "bindings": self.data["bindings"],
                    "max_output_tokens": 16000,
                    "tokenizer": "cl100k_base",
                },
            )
        )
        if packet.get("checkpoint") is None:
            raise LocalError("essential_context_unavailable")
        if (
            packet["checkpoint"]["id"] != self.data["checkpoint"]
            or packet["checkpoint"]["revision"] != self.data["revision"]
        ):
            raise LocalError("harness_checkpoint_changed")
        if canonical_hash(self.data["state"]) != packet["checkpoint"]["body_hash"]:
            raise LocalError("harness_journal_diverged")
        repository = self.data["state"]["repository"]
        if ready and repository.get("snapshot_id"):
            if not repository.get("clone"):
                raise LocalError("repository_capture_configuration_missing")
            current = self.client.repository(
                {
                    "version": 1,
                    "incarnation": self.client.binding[-1],
                    "action": "capture",
                    "key": "preaction_" + uuid.uuid4().hex,
                    "args": {
                        "project": self.client.project,
                        "repository": repository["repository"],
                        "clone": repository["clone"],
                        "environment": repository["environment"],
                    },
                }
            )
            if current["snapshot"] != repository["snapshot_id"]:
                raise LocalError("repository_snapshot_changed")
        if ready and packet["status"] != "ready":
            raise LocalError("harness_recovery_incomplete")
        return packet

    def state(self) -> dict[str, Any]:
        with self.lock:
            return copy.deepcopy(self.data["staged"] or self.data["state"])

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "checkpoint": self.data["checkpoint"],
                "revision": self.data["revision"],
                "capture": "degraded" if self.error or self.data["pending"] else "current",
                "reason": self.error,
                "staged": self.data["staged"] is not None,
                "pending_operation": None
                if self.data["operation"] is None
                else self.data["operation"]["id"],
                "activity": copy.deepcopy(self.data["activity"]),
            }

    def accept_constraint(self, text: str, *, approved: bool) -> None:
        with self.lock:
            self._guard(ready=False)
            if approved is not True:
                raise LocalError("host_authorization_required")
            state = self.state()
            if text not in state["hard_constraints"]:
                state["hard_constraints"].append(text)
            self._record("accepted_constraint", {"text": text}, state)

    def update_state(self, state: dict[str, Any], *, defer: bool = False) -> None:
        with self.lock:
            self._guard(ready=False)
            self._validate(state)
            # A host update must not erase the captured source lineage.
            if not set(map(canonical_hash, self.data["state"]["references"])) <= set(
                map(canonical_hash, state["references"])
            ):
                raise LocalError("harness_provenance_removed")
            self.data["staged"] = copy.deepcopy(state)
            self._persist()
            if not defer:
                self._record("structured_state_changed", {})

    def refresh_repository(
        self, repository: str, clone: str, environment: str, paths: list[str], *, approved: bool
    ) -> dict[str, Any]:
        """Capture registered source data and checkpoint the exact declared change scope."""
        with self.lock:
            if approved is not True:
                raise LocalError("host_authorization_required")
            self._available()
            self._flush_pending()
            # Checkpointing new bindings remains possible while old essential
            # guidance is blocked. No model or external tool executes here.
            result = self.client.repository(
                {
                    "version": 1,
                    "incarnation": self.client.binding[-1],
                    "action": "capture",
                    "key": "snapshot_" + uuid.uuid4().hex,
                    "args": {
                        "project": self.client.project,
                        "repository": repository,
                        "clone": clone,
                        "environment": environment,
                    },
                }
            )
            points = [
                {
                    "repository_id": repository,
                    "snapshot_id": result["snapshot"],
                    "commit": result["commit"],
                    "environment": environment,
                    "path": path,
                }
                for path in paths
            ]
            coverage = self.client.repository(
                {
                    "version": 1,
                    "incarnation": self.client.binding[-1],
                    "action": "coverage",
                    "args": {
                        "project": self.client.project,
                        "repository": repository,
                        "snapshot": result["snapshot"],
                        "paths": paths,
                        "bindings": points,
                    },
                }
            )
            if coverage["status"] != "complete":
                raise LocalError("binding_coverage_incomplete")
            state = self.state()
            state["repository"].update(
                repository=repository,
                clone=clone,
                current_commit=result["commit"],
                snapshot_id=result["snapshot"],
                environment=environment,
                change_scope=paths,
                dirty=result["dirty"],
            )
            if state["repository"]["base_commit"] is None:
                state["repository"]["base_commit"] = result["commit"]
            dirty = result["dirty_files"]
            old = {artifact["path"]: artifact for artifact in state["repository"]["artifacts"]}
            artifacts = []
            deleted = []
            for item in dirty:
                if item["coverage"] == "missing":
                    deleted.append(item["path"])
                    continue
                previous = old.get(item["path"])
                if (
                    previous
                    and previous["content_hash"] == item["hash"]
                    and previous["availability"] == "stored"
                ):
                    artifacts.append(previous)
                else:
                    artifacts.append(
                        {
                            "path": item["path"],
                            "availability": "missing",
                            "observation": None,
                            "content_hash": None,
                            "required": True,
                        }
                    )
            state["repository"].update(artifacts=artifacts, deleted_paths=deleted)
            self._validate(state)
            self.data["bindings"] = points
            self.data["staged"] = state
            self._persist()
            self._record(
                "repository_snapshot",
                {
                    "snapshot": result["snapshot"],
                    "generation": result["generation"],
                    "change_scope": paths,
                },
                state,
            )
            return result

    def resume(self) -> dict[str, Any]:
        with self.lock:
            return self._guard(ready=False)

    def checkpoint(self, reason: str) -> dict[str, Any]:
        with self.lock:
            self._guard(ready=False)
            return self._record("checkpoint", {"reason": reason})

    def reset_context(self) -> dict[str, Any]:
        with self.lock:
            self.checkpoint("before_context_reset")
            # Each model turn starts from a new full resume; no provider state is restored.
            return self._guard(ready=False)

    def _operations(self) -> list[dict[str, Any]]:
        return self.client.working(self._command("operations", self._scope()))["operations"]  # type: ignore[no-any-return]

    def _transition(
        self,
        row: dict[str, Any],
        state: str,
        *,
        reconciled: bool = False,
        evidence: list[dict[str, Any]] | None = None,
        result_hash: str | None = None,
    ) -> dict[str, Any]:
        return self.client.working(
            self._command(
                "operation_transition",
                {
                    "project": self.client.project,
                    "operation": row["id"],
                    "expected_revision": row["revision"],
                    "state": state,
                    "reconciled": reconciled,
                    "evidence": evidence or [],
                    "result_hash": result_hash,
                },
                row["id"] + "_" + state + "_" + str(row["revision"]),
            )
        )

    def create_file(
        self, operation: str, name: str, content: str, *, approved: bool
    ) -> dict[str, Any]:
        with self.lock:
            self._guard()
            if approved is not True:
                raise LocalError("host_authorization_required")
            repository = self.data["state"]["repository"]
            if repository.get("snapshot_id") and name not in repository.get("change_scope", []):
                raise LocalError("tool_outside_declared_scope")
            if self.data["operation"] is not None:
                raise LocalError("operation_reconciliation_required")
            if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", operation) is None:
                raise LocalError("invalid_operation_id")
            if any(item["id"] == operation for item in self._operations()):
                raise LocalError("operation_already_recorded")
            raw = self.tool.payload(operation, content)
            target = self.tool.target(name)
            self.data["operation"] = {
                "id": operation,
                "path": name,
                "expected_hash": hashlib.sha256(raw).hexdigest(),
            }
            self._persist()
            intent = self.client.working(
                self._command(
                    "operation_intent",
                    {
                        **self._scope(),
                        "operation": operation,
                        "target_hash": hashlib.sha256(str(target).encode()).hexdigest(),
                        "parameters_hash": hashlib.sha256(raw).hexdigest(),
                        "method": "readback",
                        "provider_key": None,
                    },
                    operation + "_intent",
                )
            )
            state = self.state()
            state["operation_ids"].append(operation)
            self._record(
                "tool_started",
                {
                    "operation": operation,
                    "tool": "local-file-create-v1",
                    "path": name,
                    "parameters_hash": hashlib.sha256(raw).hexdigest(),
                },
                state,
            )
            started = time.monotonic_ns()
            self._transition({"id": operation, "revision": intent["revision"]}, "dispatching")
            self.fault("before_external_effect")
            elapsed = time.monotonic_ns() - started
            if elapsed < 0 or elapsed >= 5_000_000_000:
                raise LocalError("release_expired")
            try:
                self.tool.create(name, raw)
            except Exception as exc:
                code = exc.code if isinstance(exc, LocalError) else "tool_outcome_unknown"
                evidence = self._record(
                    "tool_returned_error", {"operation": operation, "code": code}
                )
                row = next(item for item in self._operations() if item["id"] == operation)
                if code == "tool_target_exists":
                    self._transition(
                        row,
                        "failed",
                        evidence=[evidence],
                        result_hash=canonical_hash({"code": code}),
                    )
                    self._recover_operation()
                else:
                    self._transition(row, "unknown")
                raise LocalError(code) from exc
            self.fault("after_external_effect")
            return self._recover_operation()

    def _recover_operation(self) -> dict[str, Any]:
        pending = self.data["operation"]
        if pending is None:
            return {"state": "none"}
        rows = [item for item in self._operations() if item["id"] == pending["id"]]
        if not rows or rows[0]["state"] == "intent_recorded":
            self.data["operation"] = None
            self._persist()
            self._record("tool_not_dispatched", {"operation": pending["id"]})
            return {"state": "not_dispatched"}
        row = rows[0]
        if row["state"] in {"succeeded", "failed"} and not row["reconciliation_required"]:
            result = {"state": row["state"], "reason": "durable_result_already_recorded"}
        else:
            if row["state"] == "dispatching":
                transition = self._transition(row, "unknown")
                row = {**row, "state": "unknown", "revision": transition["revision"]}
            result = self.tool.inspect(pending["path"], pending["expected_hash"])
            if result["state"] == "unknown":
                return result
            evidence = self._record("tool_readback", {"operation": pending["id"], "result": result})
            self._transition(
                row,
                result["state"],
                reconciled=True,
                evidence=[evidence],
                result_hash=canonical_hash(result),
            )
        self._record("tool_completed", {"operation": pending["id"], "result": result})
        self.data["operation"] = None
        self._persist()
        return result

    def reconcile(self) -> dict[str, Any]:
        with self.lock:
            # An explicit retry of capture/readback is safe; it never invokes create.
            self.error = None
            self._flush_pending()
            self._guard(ready=False)
            return self._recover_operation()

    def model_turn(self, provider: LocalRecordingProvider | HttpAdapter) -> dict[str, str]:
        with self.lock:
            self._guard()
            self.data["activity"] = {
                "kind": "model",
                "id": "model_" + uuid.uuid4().hex,
                "outcome": "pending",
            }
            self._persist()
            name, model = "local-recording", "echo-v1"
            if isinstance(provider, HttpAdapter):
                name, model = provider.name, provider.model
            self._record("model_started", {"provider": name, "model": model})
            try:
                context = AuthorizedConversation(self.client, provider)
                context.add_memory(self._guard())
                result = context.dispatch()
            except Exception as exc:
                self.data["activity"]["outcome"] = "failed"
                self._record(
                    "model_failed",
                    {"code": exc.code if isinstance(exc, LocalError) else "provider_failure"},
                )
                self.data["activity"] = None
                self._persist()
                raise
            self.data["activity"]["outcome"] = "completed"
            # Persist observable result identity, not a free-standing copy of derived
            # restricted text that could outlive the original source permissions.
            self._record(
                "model_completed",
                {
                    "request_hash": result["request_hash"],
                    "response_hash": hashlib.sha256(result["content"].encode()).hexdigest(),
                    "permit_receipt": result["permit_receipt"],
                    "provider": name,
                    "model": model,
                },
            )
            self.data["activity"] = None
            self._persist()
            return result

    def store_artifact(self, name: str, *, approved: bool) -> dict[str, Any]:
        with self.lock:
            self._guard(ready=False)
            if approved is not True:
                raise LocalError("host_authorization_required")
            path = self.tool.target(name)
            private_file(path)
            with path.open("rb") as stream:
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise LocalError("artifact_input_limit")
            try:
                text = raw.decode("utf-8")
            except UnicodeError as exc:
                raise LocalError("artifact_not_utf8") from exc
            event = "artifact_" + uuid.uuid4().hex
            captured = self.client.capture(
                event,
                {
                    "version": 1,
                    "project": self.client.project,
                    "source": "reference-artifact-v1",
                    "source_event": event,
                    "text": text,
                    "classification": "restricted",
                    "readers": self.data["readers"],
                    "observed_at_ms": time.time_ns() // 1_000_000,
                },
            )
            artifact = {
                "path": name,
                "availability": "stored",
                "observation": captured["observation"],
                "content_hash": hashlib.sha256(raw).hexdigest(),
                "required": True,
            }
            state = self.state()
            old = [item for item in state["repository"]["artifacts"] if item["path"] != name]
            state["repository"].update(dirty=True, artifacts=[*old, artifact])
            self._record(
                "artifact_stored", {"path": name, "content_hash": artifact["content_hash"]}, state
            )
            return artifact

    def prepare_handoff(
        self, recipient: str, role: str, stage: str, *, ttl_ms: int = 60000
    ) -> dict[str, Any]:
        with self.lock:
            self._guard()
            self.checkpoint("before_handoff")
            result = self.client.handoff(
                self._command(
                    "prepare",
                    {
                        **self._scope(),
                        "checkpoint": self.data["checkpoint"],
                        "recipient": recipient,
                        "role": role,
                        "stage": stage,
                        "bindings": self.data["bindings"],
                        "ttl_ms": ttl_ms,
                    },
                    "handoff_" + uuid.uuid4().hex,
                )
            )
            self.data["handoff_checkpoint"] = self.data["checkpoint"]
            self._persist()
            return result

    def close(self, *, checkpoint: bool = True) -> None:
        self.stop.set()
        if hasattr(self, "worker"):
            self.worker.join()
        with self.lock:
            if self.closed:
                return
            try:
                if checkpoint:
                    self._guard(ready=False)
                    # The unchanged pre-handoff snapshot is also the durable shutdown
                    # snapshot. An audit-only new revision would retire that handoff.
                    if self.data["handoff_checkpoint"] != self.data["checkpoint"]:
                        self._record("shutdown", {})
            finally:
                self.closed = True
                self.owner.close()
