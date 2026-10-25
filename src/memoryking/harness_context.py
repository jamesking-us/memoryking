"""Trusted reference context and local recording provider; no network provider support.

The caller is trusted host code, never model output. Host authorization is explicit;
ordinary SDK/stdio clients do not acquire this protection automatically.
"""

from __future__ import annotations

import copy
import hashlib
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from memoryking.adapters.local_files import atomic, private_dir, private_file
from memoryking.contracts.canonical import canonical_bytes, canonical_hash, load_strict
from memoryking.domain.local import LocalError
from memoryking.sdk import BINDING_FIELDS, LocalClient

EGRESS_REVISION = "local-recording-v1"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LocalRecordingProvider:
    """A deterministic local echo with actual request files and hidden-context handles.

    It does not implement a language model or transmit any data. Records are private
    and are deliberately retained: rebuilding a conversation does not erase delivery.
    """

    def __init__(self, directory: Path):
        private_dir(directory, create=True)
        self.directory = directory

    def path(self, handle: str) -> Path:
        if re.fullmatch(r"conversation_[a-f0-9]{32}", handle) is None:
            raise LocalError("provider_handle_invalid")
        return self.directory / (handle + ".json")

    def effective_messages(self, request: dict[str, Any]) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        visited: set[str] = set()

        def expand(value: dict[str, Any]) -> None:
            for handle in value["state_handles"]:
                identifier = handle["id"]
                if identifier in visited:
                    continue
                if len(visited) >= 32:
                    raise LocalError("provider_context_limit")
                visited.add(identifier)
                path = self.path(identifier)
                try:
                    private_file(path)
                    with path.open("rb") as stream:
                        raw = stream.read(98305)
                    if len(raw) > 98304:
                        raise LocalError("provider_context_limit")
                    previous = load_strict(raw)
                except OSError as exc:
                    raise LocalError("provider_handle_unavailable") from exc
                expand(previous)
            messages.extend(value["messages"])

        expand(request)
        return messages

    def dispatch(self, raw: bytes) -> dict[str, str]:
        request = load_strict(raw)
        if (
            canonical_bytes(request) != raw
            or request["provider"] != "local-recording"
            or request["model"] != "echo-v1"
        ):
            raise LocalError("provider_request_invalid")
        self.effective_messages(request)  # Resolve the actual hidden state before accepting.
        handle = "conversation_" + uuid.uuid4().hex
        path = self.path(handle)
        atomic(path, raw)
        # This deterministic response deliberately carries input content, exercising
        # the dependency inheritance required of a real provider's derived output.
        return {
            "content": "Local echo: " + request["messages"][-1]["content"],
            "handle": handle,
            "request_path": str(path),
            "request_hash": digest(raw),
        }


class AuthorizedConversation:
    """A full-context reference dispatcher. No automatic retry or implicit rebinding."""

    def __init__(self, client: LocalClient, provider: LocalRecordingProvider):
        # An arbitrary callback could rewrite bytes, leak data, or hide remote state.
        # Additional providers need an explicitly reviewed adapter/egress policy.
        if type(provider) is not LocalRecordingProvider:
            raise LocalError("provider_not_supported")
        self._lock = threading.RLock()
        self._monotonic_ns = time.monotonic_ns
        self.client, self.provider = client, provider
        self._binding: dict[str, Any] | None = None
        self._messages: list[dict[str, str]] = []
        self._proofs: list[dict[str, Any]] = []
        self._handles: list[dict[str, str]] = []
        self._handle_proofs: list[dict[str, Any]] = []
        self.context_epoch = 0
        self.model_session_id = "session_" + uuid.uuid4().hex
        self._sync()

    def _discard(self) -> None:
        self._messages.clear()
        self._proofs.clear()
        self._handles.clear()
        self._handle_proofs.clear()
        self.context_epoch += 1
        self.model_session_id = "session_" + uuid.uuid4().hex

    def reset(self) -> None:
        """Call after the owning harness's pre-reset checkpoint has succeeded."""
        with self._lock:
            self._discard()

    def rebind(self, client: LocalClient) -> None:
        """Identity/history changes start empty; no old host requirements migrate."""
        with self._lock:
            self._discard()
            self._binding = None
            self.client = client
            self._sync()

    def _sync(self) -> dict[str, Any]:
        try:
            context = self.client.context()
        except Exception:
            self._discard()
            raise
        current = {
            key: context[key] for key in (*BINDING_FIELDS, "authorization_epoch", "purge_epoch")
        }
        current["delegation_chain"] = context.get("delegation_chain", [])
        if self._binding is not None and self._binding != current:
            self._discard()
        self._binding = current
        return {
            **current,
            "egress_policy_revision": EGRESS_REVISION,
            "model_session_id": self.model_session_id,
            "context_epoch": self.context_epoch,
        }

    def add_host(
        self,
        content: str,
        *,
        authorization_id: str,
        classification: str,
        expires_ms: int,
        approved: bool,
    ) -> None:
        """Trusted caller attests current input authority; never infer this from text."""
        with self._lock:
            self._sync()
            if approved is not True:
                raise LocalError("host_authorization_required")
            hashed = digest(content.encode())
            self._append(
                "user",
                content,
                {
                    "origin": "host",
                    "sha256": hashed,
                    "artifacts": [],
                    "host_authorizations": [
                        {
                            "id": authorization_id,
                            "classification": classification,
                            "source_hash": hashed,
                            "expires_ms": expires_ms,
                        }
                    ],
                },
            )

    def add_memory(self, packet: dict[str, Any]) -> None:
        with self._lock:
            self._sync()
            if (
                packet.get("status") in {"blocked", "budget_insufficient", "incomplete"}
                or "receipt" not in packet
            ):
                raise LocalError("essential_context_unavailable")
            raw = canonical_bytes(packet)
            self._append(
                "system",
                raw.decode(),
                {
                    "origin": "memory",
                    "sha256": digest(raw),
                    "artifacts": [{"receipt": packet["receipt"], "sha256": digest(raw)}],
                    "host_authorizations": [],
                },
            )

    def _append(self, role: str, content: str, proof: dict[str, Any]) -> None:
        if not content or len(content) > 65536 or len(self._messages) >= 31:
            raise LocalError("inference_request_limit")
        self._messages.append({"role": role, "content": content})
        self._proofs.append(proof)

    def _args(self, binding: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(
            {
                "binding": binding,
                "request": {
                    "provider": "local-recording",
                    "model": "echo-v1",
                    "messages": self._messages,
                    "state_handles": self._handles,
                },
                "manifest": {"segments": self._proofs, "handles": self._handle_proofs},
            }
        )

    @staticmethod
    def _inherited(proofs: list[dict[str, Any]], raw: bytes) -> dict[str, Any]:
        closure: dict[str, list[Any]] = {}
        for name in ("artifacts", "host_authorizations"):
            unique = {canonical_hash(value): value for proof in proofs for value in proof[name]}
            closure[name] = [unique[key] for key in sorted(unique)]
        return {"origin": "derived", "sha256": digest(raw), **closure}

    def dispatch(self) -> dict[str, str]:
        with self._lock:
            args = self._args(self._sync())
            if not args["request"]["messages"]:
                raise LocalError("context_rebuild_required")
            if len(args["request"]["messages"]) >= 31:
                raise LocalError("inference_request_limit")
            closure = self._inherited(
                args["manifest"]["segments"] + args["manifest"]["handles"], b""
            )
            if len(closure["artifacts"]) > 32 or len(closure["host_authorizations"]) > 32:
                raise LocalError("inference_request_limit")
            # Freeze one byte string. The provider sees these exact bytes only.
            raw = canonical_bytes(args["request"])
            started = self._monotonic_ns()
            try:
                permit = self.client.inference_status(
                    {"version": 1, "action": "authorize", "args": args}
                )
                used = self.client.inference_status(
                    {
                        "version": 1,
                        "action": "consume",
                        "args": {**args, "permit": permit["permit"]},
                    }
                )
                if used["status"] != "consumed" or used["request_hash"] != digest(raw):
                    raise LocalError("inference_permit_mismatch")
                elapsed = self._monotonic_ns() - started
                if elapsed < 0:
                    raise LocalError("clock_untrusted")
                if elapsed >= 5_000_000_000:
                    raise LocalError("release_expired")
                result = self.provider.dispatch(raw)
            except Exception:
                self._discard()
                raise
            # Never make an in-flight response available to the next tool/model turn
            # until the complete derived closure passes a new, current validation.
            try:
                current = self._sync()
                if current != args["binding"]:
                    raise LocalError("inference_context_changed")
                inherited = args["manifest"]["segments"] + args["manifest"]["handles"]
                self._append(
                    "assistant",
                    result["content"],
                    self._inherited(inherited, result["content"].encode()),
                )
                handle = {"kind": "conversation", "id": result["handle"]}
                self._handles = [handle]
                self._handle_proofs = [self._inherited(inherited, canonical_bytes(handle))]
                checked_at = self._monotonic_ns()
                validation = self.client.inference_status(
                    {"version": 1, "action": "validate", "args": self._args(current)}
                )
                if validation["status"] != "validated":
                    raise LocalError("inference_provenance_unknown")
                elapsed = self._monotonic_ns() - checked_at
                if elapsed < 0 or elapsed >= 5_000_000_000:
                    raise LocalError("release_expired")
            except Exception as exc:
                self._discard()
                raise LocalError("inference_result_quarantined") from exc
            return {**result, "status": "validated", "permit_receipt": used["receipt"]}
