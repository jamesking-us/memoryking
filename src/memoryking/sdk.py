"""Small local SDK with explicit binding, current checks and no automatic mutation retry."""

from __future__ import annotations

import json
import secrets
import time
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
from memoryking.domain.checkpoint import complete
from memoryking.domain.local import LocalError

CATALOG = json.loads(Path(__file__).with_name("adapters").joinpath("stdio.tools.json").read_text())
TOOL_VALIDATORS = {
    item["name"]: Draft202012Validator(item["inputSchema"]) for item in CATALOG["tools"]
}
DEFAULT_BUDGET = 6000
MUTATION_TOOLS = {"memory_checkpoint", "memory_handoff", "memory_feedback"}
PLAN_MUTATIONS = {"plan_save", "plan_step"}


def _resolve(node: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    while isinstance(ref, str) and ref.startswith("#/$defs/"):
        node = defs[ref.removeprefix("#/$defs/")]
        ref = node.get("$ref")
    return node


def _tool_args() -> dict[tuple[str, Any], set[str]]:
    """Which args each tool action accepts, so defaults are filled only where they fit."""
    result: dict[tuple[str, Any], set[str]] = {}
    for item in CATALOG["tools"]:
        defs = item["inputSchema"].get("$defs", {})
        for branch in item["inputSchema"].get("oneOf", []):
            action = branch["properties"].get("action", {}).get("const")
            args = _resolve(branch["properties"].get("args", {}), defs)
            properties = args.get("properties", {})
            result[(item["name"], action)] = set(properties)
            nested = properties.get("brief")
            if isinstance(nested, dict):
                result[(item["name"], "brief")] = set(_resolve(nested, defs).get("properties", {}))
    return result


TOOL_ARGS = _tool_args()
_TOKENIZER: list[str] = []


def default_tokenizer() -> str:
    """The exact tokenizer when the optional lock is installed, else the disclosed estimate."""
    if not _TOKENIZER:
        from memoryking.adapters.token_count import encoding

        try:
            encoding()
            _TOKENIZER.append("cl100k_base")
        except LocalError:
            _TOKENIZER.append("utf8-byte-estimate-v1")
    return _TOKENIZER[0]


BINDING_FIELDS = ("store", "tenant", "principal", "project", "incarnation")


class LocalClient:
    """One project and identity/history per instance. Recreate explicitly after a context change.

    Ordinary memory calls cannot remove data retained by a consuming host. The separate
    inference_status method is a trusted host surface; use AuthorizedConversation to
    enforce exact-byte dispatch, dependency inheritance and conversation rebuilding.
    """

    def __init__(self, socket: Path, credential_file: Path, project: str, namespace: str = "sdk"):
        self.socket, self.credential_file, self.project = socket, credential_file, project
        # A named namespace makes `key` a real idempotency key across processes.
        self.namespace = namespace
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

    def report(self, command: dict[str, Any]) -> dict[str, Any]:
        """Scoped report surface. A viewer credential cannot author or deliver reports."""
        from memoryking.contracts.viewer import validate_viewer

        validate_viewer("ReportCommand", command, reporting=True)
        self._bound(command)
        credential, _ = self._current()
        return self._request(credential, "report", {"command": command})

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

    def inspection(self, command: dict[str, Any]) -> dict[str, Any]:
        """A bounded inspection (`catalog`, `history`, `cost`, ...) over the socket."""
        self._bound(command)
        credential, _ = self._current()
        return self._request(credential, "inspection", {"command": command})

    def knowledge(self, command: dict[str, Any]) -> dict[str, Any]:
        """A knowledge command (`propose`, `query`, `review`, ...); the store validates it."""
        self._bound(command)
        credential, _ = self._current()
        return self._request(credential, "knowledge", {"command": command})

    def purge(self, observation: str) -> None:
        """Erase one observation and what was built from it; denied without an admin grant."""
        credential, _ = self._current()
        self._request(credential, "purge", {"observation": observation})

    def capture(self, key: str, observation: dict[str, Any]) -> dict[str, Any]:
        if observation.get("project") != self.project:
            raise LocalError("scope_mismatch")
        credential, _ = self._current()
        namespace = self._request(
            credential, "namespace", {"project": self.project, "name": self.namespace}
        )["namespace"]
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
        args = command["args"]
        project = args["binding"]["project"] if "binding" in args else args.get("project")
        if project != self.project:
            raise LocalError("scope_mismatch")
        credential, _ = self._current()
        return self._request(credential, "inference", {"command": command})

    def defaults(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Fill what the catalog leaves optional from the session binding.

        The store still validates the completed command, so a caller who sends every
        field explicitly gets exactly the old behavior.
        """
        arguments = {**arguments}
        if name == "memory_status":
            arguments.setdefault("project", self.project)
            return arguments
        if name == "memory_capture":
            request = arguments.get("request")
            if isinstance(request, dict):
                arguments["request"] = {"version": 1, "project": self.project, **request}
            return arguments
        arguments = {"version": 1, "incarnation": self.binding[-1], **arguments}
        plan_write = name == "memory_plan" and arguments.get("action") in PLAN_MUTATIONS
        if (name in MUTATION_TOOLS or plan_write) and "key" not in arguments:
            # Without a caller-supplied key there is nothing to replay, so a fresh key
            # keeps the call valid; supply a stable key to make retries idempotent.
            arguments["key"] = "auto-" + secrets.token_hex(12)
        args = arguments.get("args")
        if isinstance(args, dict):
            arguments["args"] = self._default_args(name, arguments.get("action"), args)
        return arguments

    def _default_args(self, name: str, action: Any, args: dict[str, Any]) -> dict[str, Any]:
        args = {**args}
        fields = TOOL_ARGS.get((name, action), set())
        if name == "memory_checkpoint" and action == "save":
            # Progressive checkpoints: a minimal body saves in one call, the gaps are
            # recorded, and a missing expected_revision accepts the current one.
            args.setdefault("run", None)
            args.setdefault("readers", None)  # keep the stream's readers (writer on first save)
            args.setdefault("expected_revision", None)
            if isinstance(args.get("checkpoint"), dict):
                args["checkpoint"] = complete(args["checkpoint"], int(time.time() * 1000))
        if name == "memory_resume" and action == "resume":
            args.setdefault("run", None)
        if "project" in fields:
            args.setdefault("project", self.project)
        if "bindings" in fields:
            args.setdefault("bindings", [{}])
        if "max_output_tokens" in fields:
            args.setdefault("max_output_tokens", DEFAULT_BUDGET)
        if "tokenizer" in fields:
            args.setdefault("tokenizer", default_tokenizer())
        nested = args.get("brief")
        if "brief" in fields and isinstance(nested, dict):
            args["brief"] = self._default_args(name, "brief", nested)
        return args

    def tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        validator = TOOL_VALIDATORS.get(name)
        if validator is None:
            raise LocalError("unknown_tool")
        if not validator.is_valid(arguments):
            raise LocalError("invalid_tool_arguments")
        arguments = self.defaults(name, arguments)
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
