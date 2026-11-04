"""Provider adapters: one bounded HTTPS call per dispatch, nothing else.

An adapter is constructed from the operator's egress policy and turns the frozen
canonical request bytes into one provider request. It cannot add, drop or reorder
messages: the mapping to the wire body is a fixed function per adapter, and the hash of
the exact wire body is returned so the daemon's audit record can hold it. One attempt,
a bounded timeout, a bounded reply, no streaming, no tool use, no retained state.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import socket
import ssl
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memoryking.adapters.egress_policy import EgressPolicy, ProviderPolicy
from memoryking.adapters.local_files import private_file
from memoryking.contracts.canonical import canonical_bytes, load_strict
from memoryking.domain.local import LocalError

RESPONSE_LIMIT = 1048576
ANTHROPIC_VERSION = "2023-06-01"
VERTEX_ANTHROPIC_VERSION = "vertex-2023-10-16"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class ProviderResult:
    content: str
    request_hash: str
    wire_hash: str
    usage: dict[str, int | None]


class HttpAdapter:
    """Shared transport and credential handling; subclasses define the wire mapping."""

    stateful = False

    def __init__(self, policy: EgressPolicy, entry: ProviderPolicy, model: str):
        if not entry.allows(model):
            raise LocalError("egress_provider_denied")
        self.policy_revision = policy.binding
        self.last_wire_hash: str | None = None  # the body last handed to the transport
        self.entry = entry
        self.name = entry.name
        self.model = model
        self.host = entry.host

    # Credentials are read by the host process at dispatch time and never logged.
    def _secret(self, path: Path | None) -> str | None:
        if path is None:
            return None
        try:
            private_file(path)
            value = path.read_text().strip()
        except (OSError, LocalError) as exc:
            raise LocalError("provider_credential_missing") from exc
        if not value or len(value) > 8192 or any(c in value for c in "\r\n"):
            raise LocalError("provider_credential_missing")
        return value

    def _token(self) -> str:
        entry = self.entry
        if entry.token_file is not None:
            return self._secret(entry.token_file) or ""
        if entry.token_command is not None:
            try:
                completed = subprocess.run(
                    list(entry.token_command),
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                    env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(Path.home())},
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise LocalError("provider_credential_missing") from exc
            token = completed.stdout.strip()
            if completed.returncode or not token or len(token) > 8192:
                raise LocalError("provider_credential_missing")
            return token
        raise LocalError("provider_credential_missing")

    def _post(self, path: str, headers: dict[str, str], body: bytes) -> dict[str, Any]:
        entry = self.entry
        connection: http.client.HTTPConnection
        if entry.insecure_http:
            connection = http.client.HTTPConnection(entry.host, timeout=entry.timeout_s)
        else:
            connection = http.client.HTTPSConnection(
                entry.host, timeout=entry.timeout_s, context=ssl.create_default_context()
            )
        try:
            connection.request(
                "POST",
                path,
                body,
                {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Content-Length": str(len(body)),
                    **entry.headers,
                    **headers,
                },
            )
            response = connection.getresponse()
            raw = response.read(RESPONSE_LIMIT + 1)
        except (OSError, http.client.HTTPException, socket.timeout, ssl.SSLError) as exc:
            raise LocalError("provider_unavailable") from exc
        finally:
            connection.close()
        if response.status != 200:
            raise LocalError("provider_unavailable")
        if len(raw) > RESPONSE_LIMIT:
            raise LocalError("provider_response_invalid")
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise LocalError("provider_response_invalid") from exc
        if not isinstance(value, dict):
            raise LocalError("provider_response_invalid")
        return value

    def dispatch(self, raw: bytes) -> ProviderResult:
        self.last_wire_hash = None
        request = load_strict(raw)
        if (
            not isinstance(request, dict)
            or canonical_bytes(request) != raw
            or request.get("provider") != self.name
            or request.get("model") != self.model
            or request.get("state_handles") != []
        ):
            raise LocalError("provider_request_invalid")
        body = canonical_bytes(self.wire(request))
        # Recorded before the transport so a failed delivery still audits the bytes
        # that may have left.
        self.last_wire_hash = digest(body)
        reply = self._post(self.path(), self.headers(), body)
        content, usage = self.parse(reply)
        if not isinstance(content, str) or not content or len(content) > 65536:
            raise LocalError("provider_response_invalid")
        return ProviderResult(content, digest(raw), digest(body), usage)

    def path(self) -> str:
        raise NotImplementedError

    def headers(self) -> dict[str, str]:
        raise NotImplementedError

    def wire(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def parse(self, reply: dict[str, Any]) -> tuple[str, dict[str, int | None]]:
        raise NotImplementedError


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _anthropic_body(request: dict[str, Any], max_tokens: int) -> dict[str, Any]:
    system = [m["content"] for m in request["messages"] if m["role"] == "system"]
    messages = [
        {"role": "assistant" if m["role"] == "assistant" else "user", "content": m["content"]}
        for m in request["messages"]
        if m["role"] != "system"
    ]
    body: dict[str, Any] = {"max_tokens": max_tokens, "messages": messages}
    if system:
        body["system"] = "\n\n".join(system)
    return body


def _anthropic_parse(reply: dict[str, Any]) -> tuple[str, dict[str, int | None]]:
    blocks = reply.get("content")
    if not isinstance(blocks, list):
        raise LocalError("provider_response_invalid")
    text = "".join(
        b["text"] for b in blocks if isinstance(b, dict) and isinstance(b.get("text"), str)
    )
    usage: dict[str, Any] = reply["usage"] if isinstance(reply.get("usage"), dict) else {}
    return text, {
        "input_tokens": _count(usage.get("input_tokens")),
        "output_tokens": _count(usage.get("output_tokens")),
    }


class AnthropicMessages(HttpAdapter):
    def path(self) -> str:
        return self.entry.path or "/v1/messages"

    def headers(self) -> dict[str, str]:
        key = self._secret(self.entry.key_file)
        if key is None:
            raise LocalError("provider_credential_missing")
        return {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}

    def wire(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"model": self.model, **_anthropic_body(request, self.entry.max_output_tokens)}

    def parse(self, reply: dict[str, Any]) -> tuple[str, dict[str, int | None]]:
        return _anthropic_parse(reply)


class OpenAIChat(HttpAdapter):
    """Chat completions: OpenAI, xAI Grok, and self-hosted servers at an explicit host."""

    def path(self) -> str:
        return self.entry.path or "/v1/chat/completions"

    def headers(self) -> dict[str, str]:
        key = self._secret(self.entry.key_file)
        return {"Authorization": "Bearer " + key} if key else {}

    def wire(self, request: dict[str, Any]) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": self.entry.max_output_tokens,
            "messages": [
                {"role": "user" if m["role"] == "tool" else m["role"], "content": m["content"]}
                for m in request["messages"]
            ],
            "stream": False,
        }

    def parse(self, reply: dict[str, Any]) -> tuple[str, dict[str, int | None]]:
        choices = reply.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise LocalError("provider_response_invalid")
        message = choices[0].get("message")
        content = message.get("content") if isinstance(message, dict) else None
        usage: dict[str, Any] = reply["usage"] if isinstance(reply.get("usage"), dict) else {}
        return content if isinstance(content, str) else "", {
            "input_tokens": _count(usage.get("prompt_tokens")),
            "output_tokens": _count(usage.get("completion_tokens")),
        }


class VertexAI(HttpAdapter):
    """Google Cloud Vertex AI: Gemini through generateContent, or Anthropic through rawPredict."""

    def _base(self) -> str:
        entry = self.entry
        return (
            f"/v1/projects/{entry.project}/locations/{entry.location}"
            f"/publishers/{entry.publisher}/models/{self.model}"
        )

    def path(self) -> str:
        if self.entry.path:
            return self.entry.path
        return self._base() + (
            ":rawPredict" if self.entry.publisher == "anthropic" else ":generateContent"
        )

    def headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer " + self._token()}

    def wire(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.entry.publisher == "anthropic":
            return {
                "anthropic_version": VERTEX_ANTHROPIC_VERSION,
                **_anthropic_body(request, self.entry.max_output_tokens),
            }
        system = [m["content"] for m in request["messages"] if m["role"] == "system"]
        body: dict[str, Any] = {
            "contents": [
                {
                    "role": "model" if m["role"] == "assistant" else "user",
                    "parts": [{"text": m["content"]}],
                }
                for m in request["messages"]
                if m["role"] != "system"
            ],
            "generationConfig": {"maxOutputTokens": self.entry.max_output_tokens},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system)}]}
        return body

    def parse(self, reply: dict[str, Any]) -> tuple[str, dict[str, int | None]]:
        if self.entry.publisher == "anthropic":
            return _anthropic_parse(reply)
        candidates = reply.get("candidates")
        if (
            not isinstance(candidates, list)
            or not candidates
            or not isinstance(candidates[0], dict)
        ):
            raise LocalError("provider_response_invalid")
        content = candidates[0].get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        text = "".join(
            p["text"]
            for p in (parts if isinstance(parts, list) else [])
            if isinstance(p, dict) and isinstance(p.get("text"), str)
        )
        usage: dict[str, Any] = (
            reply["usageMetadata"] if isinstance(reply.get("usageMetadata"), dict) else {}
        )
        return text, {
            "input_tokens": _count(usage.get("promptTokenCount")),
            "output_tokens": _count(usage.get("candidatesTokenCount")),
        }


ADAPTERS: dict[str, type[HttpAdapter]] = {
    "anthropic-messages": AnthropicMessages,
    "openai-chat": OpenAIChat,
    "vertex-ai": VertexAI,
}


def adapter(policy: EgressPolicy, name: str, model: str) -> HttpAdapter:
    """The adapter the policy permits for a provider and model, or egress_provider_denied."""
    entry = policy.provider(name)
    if entry is None or name not in ADAPTERS:
        raise LocalError("egress_provider_denied")
    return ADAPTERS[name](policy, entry, model)
