"""OpenAI-compatible function calling for the lean tools, with no third-party dependency.

For anyone with their own agent loop and any provider that takes JSON-schema function
definitions. `FunctionTools.definitions()` lists the tools; `FunctionTools.call(name,
arguments_json)` runs one through the bound client and returns the text a model should see.
Every call goes through `memoryking.lean.call`, the code path of the lean MCP tools, so
authorization, receipts, idempotency and budgets are unchanged. Nothing here decides what
is remembered or briefed: it validates arguments against the tool's own schema, runs the
call and renders the reply.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Literal

from jsonschema import Draft202012Validator

from memoryking import lean
from memoryking.contracts.canonical import ContractError
from memoryking.domain.hints import hint
from memoryking.domain.local import LocalError
from memoryking.sdk import LocalClient

EVERYDAY = ("remember", "recall", "save_progress", "resume", "hand_off")
"""The five everyday operations; the default tool set of every adapter."""

REVIEW = ("propose", "confirm", "correct", "forget")
"""Reviewing and correcting memory; opt in by name. `confirm` also needs `chat_review=True`."""

# What a model sees for the tools whose reply is not Markdown: the fields of the typed result.
SHOWN = {
    "remember": ("observation", "position", "held"),
    "save_progress": ("checkpoint", "revision", "status", "receipt", "replayed"),
    "hand_off": ("status", "handle"),
    "propose": ("proposal", "state"),
    "confirm": ("proposal", "state"),
    "correct": ("observation", "position", "held"),
    "forget": None,
}
_CATALOG = {item["name"]: item for item in lean.TOOLS}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _failure(code: str) -> str:
    return _json({"error": code, "hint": hint(code)})


def render(name: str, reply: dict[str, Any]) -> str:
    """Cited Markdown for `recall` and `resume`; a few fields as compact JSON for the rest."""
    if name in {"recall", "resume"} and isinstance(reply.get("text"), str) and reply["text"]:
        return str(reply["text"])
    fields = SHOWN.get(name)
    return _json(reply if fields is None else {key: reply[key] for key in fields if key in reply})


class FunctionTools:
    """The lean tools bound to one client.

    `names` selects the tools (default: the five everyday ones). `chat_review` is the
    operator's switch for `confirm`, as `--allow-chat-review` is for the bridge; a model
    cannot set it.
    """

    def __init__(
        self,
        client: LocalClient,
        names: Sequence[str] = EVERYDAY,
        *,
        chat_review: bool = False,
    ):
        unknown = [name for name in names if name not in _CATALOG]
        if unknown:
            raise LocalError("unknown_tool", hint("unknown_tool"))
        self.client = client
        self.names = tuple(names)
        self.chat_review = chat_review
        self._validators = {
            name: Draft202012Validator(_CATALOG[name]["inputSchema"]) for name in self.names
        }

    def specs(self) -> list[dict[str, Any]]:
        """Name, description and JSON-schema parameters of each tool, copied from the catalog."""
        return [
            {
                "name": name,
                "description": _CATALOG[name]["description"],
                "parameters": json.loads(json.dumps(_CATALOG[name]["inputSchema"])),
            }
            for name in self.names
        ]

    def definitions(self, style: Literal["chat", "responses"] = "chat") -> list[dict[str, Any]]:
        """Function definitions for a provider's `tools` argument.

        `chat` is the nested Chat Completions shape; `responses` is the flat shape. Both are
        the common OpenAI-compatible forms; the schemas are not strict-mode, because the
        lean tools have optional fields with defaults.
        """
        if style == "chat":
            return [{"type": "function", "function": spec} for spec in self.specs()]
        return [{"type": "function", **spec} for spec in self.specs()]

    def execute(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Run one tool and return `(text, is_error)`.

        A refusal comes back as `{"error": code, "hint": ...}` text, not a raise: that is what
        a model can act on. A program that wants exceptions calls the client methods."""
        validator = self._validators.get(name)
        try:
            if validator is None:
                raise LocalError("unknown_tool")
            if not isinstance(arguments, dict) or not validator.is_valid(arguments):
                raise LocalError("invalid_tool_arguments")
            reply = lean.call(self.client, name, arguments, chat_review=self.chat_review)
        except LocalError as exc:
            return _failure(exc.code), True
        except ContractError:
            return _failure("invalid_tool_arguments"), True
        return render(name, reply), False

    def run(self, name: str, arguments: dict[str, Any]) -> str:
        """The text a model should see for one tool call (see `execute`)."""
        return self.execute(name, arguments)[0]

    def call(self, name: str, arguments_json: str | bytes | None) -> str:
        """Run a model's tool call: `arguments_json` is the JSON text the provider returned."""
        try:
            arguments = json.loads(arguments_json) if arguments_json else {}
        except ValueError:
            return _failure("invalid_tool_arguments")
        return self.run(name, arguments)
