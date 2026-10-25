"""Small local SDK with explicit binding, current checks and no automatic mutation retry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from memoryking.adapters.local_files import private_file
from memoryking.adapters.local_ipc import request
from memoryking.contracts.validation import (
    validate_embedding,
    validate_experience,
    validate_handoff,
    validate_inference,
    validate_processing,
    validate_repository,
    validate_retrieval,
    validate_working,
)
from memoryking.domain.local import LocalError

CATALOG = json.loads(Path(__file__).with_name("adapters").joinpath("stdio.tools.json").read_text())
TOOL_VALIDATORS = {
    item["name"]: Draft202012Validator(item["inputSchema"]) for item in CATALOG["tools"]
}
BINDING_FIELDS = ("store", "tenant", "principal", "project", "incarnation")


class LocalClient:
    """One project and identity/history per instance. Recreate explicitly after a context change.

    Ordinary memory calls cannot remove data retained by a consuming host. The separate
    inference_status method is a trusted host surface; use AuthorizedConversation to
    enforce exact-byte dispatch, dependency inheritance and conversation rebuilding.
    """

    def __init__(self, socket: Path, credential_file: Path, project: str):
        self.socket, self.credential_file, self.project = socket, credential_file, project
        self.last_context = self._request(self._credential(), "context", {"project": project})
        self.binding = tuple(self.last_context[key] for key in BINDING_FIELDS)

    def _credential(self) -> str:
        private_file(self.credential_file)
        try:
            with self.credential_file.open("rb") as source:
                raw = source.read(8193)
            if len(raw) > 8192 or not raw.strip():
                raise LocalError("invalid_token")
            return raw.decode("utf-8").strip()
        except UnicodeError as exc:
            raise LocalError("invalid_token") from exc

    def _request(self, credential: str, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            return request(self.socket, credential, operation, args)
        except OSError as exc:
            raise LocalError("transport_unavailable") from exc

    def _current(self) -> tuple[str, dict[str, Any]]:
        credential = self._credential()
        context = self._request(credential, "context", {"project": self.project})
        if tuple(context[key] for key in BINDING_FIELDS) != self.binding:
            raise LocalError("context_changed")
        self.last_context = context
        return credential, context

    def context(self) -> dict[str, Any]:
        return self._current()[1]

    def _bound(self, command: dict[str, Any]) -> None:
        if command["incarnation"] != self.binding[-1]:
            raise LocalError("history_changed")
        if command["args"]["project"] != self.project:
            raise LocalError("scope_mismatch")

    def retrieval(self, command: dict[str, Any]) -> dict[str, Any]:
        validate_retrieval(command)
        self._bound(command)
        credential, _ = self._current()
        return self._request(credential, "retrieval", {"command": command})

    def atlas(self, *, bindings: list[dict[str, Any]], **options: Any) -> dict[str, Any]:
        """Read the bounded graph/list for a host UI; not a model-facing MCP tool.

        Labels are excerpts for inspection. Counts refer only to returned nodes,
        and truncation never establishes absence outside the authorized window.
        """
        return self.retrieval(
            {
                "version": 1,
                "incarnation": self.binding[-1],
                "action": "atlas",
                "args": {**options, "project": self.project, "bindings": bindings},
            }
        )

    def repository(self, command: dict[str, Any]) -> dict[str, Any]:
        """Trusted host repository surface; no registration in the model tool catalog."""
        validate_repository(command)
        self._bound(command)
        credential, _ = self._current()
        return self._request(credential, "repository", {"command": command})

    def embedding(self, command: dict[str, Any]) -> dict[str, Any]:
        """Trusted operator/worker surface, excluded from model-facing MCP tools."""
        validate_embedding(command)
        self._bound(command)
        credential, _ = self._current()
        return self._request(credential, "embedding", {"command": command})

    def processing(self, command: dict[str, Any]) -> dict[str, Any]:
        """Trusted operator/worker surface, excluded from model-facing MCP tools."""
        validate_processing(command)
        self._bound(command)
        credential, _ = self._current()
        return self._request(credential, "processing", {"command": command})

    def experience(self, command: dict[str, Any]) -> dict[str, Any]:
        """Experience surface; MCP exposes only outcome and feedback, never activation."""
        validate_experience(command)
        self._bound(command)
        credential, _ = self._current()
        return self._request(credential, "experience", {"command": command})

    def working(self, command: dict[str, Any]) -> dict[str, Any]:
        validate_working(command)
        self._bound(command)
        if command["action"] == "assign_writer":
            raise LocalError("administrative_operation_required")
        credential, _ = self._current()
        return self._request(credential, "working", {"command": command})

    def capture(self, key: str, observation: dict[str, Any]) -> dict[str, Any]:
        if observation.get("project") != self.project:
            raise LocalError("scope_mismatch")
        credential, _ = self._current()
        namespace = self._request(credential, "namespace", {"project": self.project})["namespace"]
        return self._request(
            credential, "capture", {"namespace": namespace, "key": key, "request": observation}
        )

    def handoff(self, command: dict[str, Any]) -> dict[str, Any]:
        validate_handoff(command)
        self._bound(command)
        credential, _ = self._current()
        return self._request(credential, "handoff", {"command": command})

    def inference_status(self, command: dict[str, Any]) -> dict[str, Any]:
        """Trusted host surface; deliberately absent from the model-facing tool catalog."""
        validate_inference(command)
        if command["args"]["binding"]["project"] != self.project:
            raise LocalError("scope_mismatch")
        credential, _ = self._current()
        return self._request(credential, "inference", {"command": command})

    def tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        validator = TOOL_VALIDATORS.get(name)
        if validator is None:
            raise LocalError("unknown_tool")
        if not validator.is_valid(arguments):
            raise LocalError("invalid_tool_arguments")
        if name == "memory_status":
            if arguments["project"] != self.project:
                raise LocalError("scope_mismatch")
            return self.context()
        if name == "memory_capture":
            return self.capture(arguments["key"], arguments["request"])
        if name in {"memory_brief", "memory_fetch"}:
            return self.retrieval(arguments)
        if name == "memory_handoff":
            return self.handoff(arguments)
        if name == "memory_feedback":
            return self.experience(arguments)
        return self.working(arguments)
