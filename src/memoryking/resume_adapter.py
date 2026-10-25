"""Host adapter reconstructing full essential context from optional transport deltas."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from memoryking.adapters.inference import EGRESS_REVISION
from memoryking.contracts.canonical import ContractError, canonical_bytes, canonical_hash
from memoryking.domain.delta import SCHEMA, materialize
from memoryking.domain.local import LocalError
from memoryking.sdk import LocalClient

if TYPE_CHECKING:
    from memoryking.harness_context import AuthorizedConversation


class ResumeAdapter:
    """In-memory authorized state only; never evidence of model context retention."""

    def __init__(self, client: LocalClient, model_session: str, context_epoch: int = 0):
        self.client = client
        self.model_session, self.context_epoch = model_session, context_epoch
        self._target: dict[str, Any] | None = None
        self._base: dict[str, Any] | None = None
        self._binding: dict[str, Any] | None = None
        self.last_envelope: dict[str, Any] | None = None
        self.transfers: list[dict[str, Any]] = []

    def discard(self) -> None:
        self._target = self._base = self._binding = None

    def reset_model_context(self, model_session: str) -> None:
        self.model_session = model_session
        self.context_epoch += 1
        self.discard()

    def resume_for(self, conversation: AuthorizedConversation, **options: Any) -> dict[str, Any]:
        """Reference-host integration: sync resets/revocations before any base reuse."""
        if conversation.client is not self.client:
            self.discard()
            raise LocalError("context_changed")
        with conversation._lock:
            context = conversation._sync()
            self.model_session = context["model_session_id"]
            self.context_epoch = context["context_epoch"]
            return self.resume(**options)

    def resume(
        self,
        *,
        task: str,
        bindings: list[dict[str, Any]],
        max_output_tokens: int,
        tokenizer: str,
        run: str | None = None,
        checkpoint: str | None = None,
    ) -> dict[str, Any]:
        args = {
            "project": self.client.project,
            "task": task,
            "run": run,
            "checkpoint": checkpoint,
            "bindings": bindings,
            "max_output_tokens": max_output_tokens,
            "tokenizer": tokenizer,
            "adapter_context": {
                "model_session": self.model_session,
                "context_epoch": self.context_epoch,
            },
        }
        self.transfers = []
        for attempt in range(2):
            try:
                context = self.client.context()
                binding = {
                    key: context[key]
                    for key in (
                        "store",
                        "tenant",
                        "principal",
                        "incarnation",
                        "project",
                        "authorization_epoch",
                        "purge_epoch",
                        "delegation_chain",
                    )
                }
                binding.update(
                    egress_policy_revision=EGRESS_REVISION,
                    model_session=self.model_session,
                    context_epoch=self.context_epoch,
                    view_hash=canonical_hash(
                        {
                            key: args[key]
                            for key in (
                                "project",
                                "task",
                                "run",
                                "checkpoint",
                                "bindings",
                                "tokenizer",
                            )
                        }
                    ),
                )
                if self._binding != binding:
                    self.discard()
                envelope = self.client.working(
                    {
                        "version": 1,
                        "incarnation": self.client.binding[-1],
                        "action": "adapter_resume",
                        "args": {**args, "base": self._base},
                    }
                )
            except Exception:
                self.discard()
                raise
            self.last_envelope = copy.deepcopy(envelope)
            self.transfers.append(
                {
                    "mode": envelope.get("mode"),
                    "bytes": len(canonical_bytes(envelope)),
                    "tokens": envelope.get("usage", {}).get("output_tokens"),
                    "attempt": attempt + 1,
                }
            )
            if envelope.get("status") == "budget_insufficient":
                self.discard()
                raise LocalError("budget_insufficient")
            try:
                target = materialize(envelope, self._target, binding)
            except (LocalError, ContractError, KeyError, TypeError, ValueError):
                self.discard()
                if attempt == 0:
                    continue  # One fresh authorized full compilation, never old full bytes.
                raise LocalError("delta_reconstruction_failed") from None
            self._target = target
            self._binding = binding
            self._base = (
                {
                    "id": envelope["base_id"],
                    "hash": envelope["target_hash"],
                    "sequence": envelope["sequence"],
                    "schema": SCHEMA,
                }
                if envelope["base_id"]
                else None
            )
            # The model sees the same complete packet as the ordinary resume path,
            # with its own retained full artifact for exact inference authorization.
            return copy.deepcopy(target["packet"])
        raise LocalError("delta_reconstruction_failed")
