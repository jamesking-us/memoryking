"""Cookie UI boundary: session validation inside each domain authorization decision."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Any, Protocol, cast

from memoryking.application.store_core import StoreCore
from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import LocalError, Released


class CommandService(Protocol):
    def execute(self, credential: str, command: dict[str, Any]) -> Released: ...


class BrowserStore:
    """Request-local facade; never mutate the shared store's authentication method.

    The domain opens its own transaction and passes its persisted evaluation instant
    to _auth. Session expiry/epoch and operation capability are checked at that same
    instant. Subject/intersection checks keep the real store's identity semantics.
    """

    def __init__(self, store: StoreCore, session: dict[str, Any], check_session: Callable[[], Any]):
        self.store, self.session = store, dict(session)
        self.check_session = check_session

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)

    def _auth(self, credential: str, capability: str, project: str, at: int) -> dict[str, Any]:
        value, store = self.session, self.store
        self.check_session()
        if credential != value["credential"]:
            raise LocalError("mixed_authentication")
        who = store._auth(credential, "admin", "*", at)
        epoch = store.db.execute(
            "SELECT policy_epoch FROM tenants WHERE tenant=?", (who["tenant"],)
        ).fetchone()[0]
        if (
            at >= value["expires"]
            or value["epoch"] != epoch
            or value["incarnation"] != store.incarnation
            or who["tenant"] != value["tenant"]
        ):
            raise LocalError("session_changed")
        return store._auth(credential, capability, project, at)


def execute(
    store: StoreCore,
    session: dict[str, Any],
    body: dict[str, Any],
    check_session: Callable[[], Any],
) -> Released:
    from memoryking.adapters.experience import Experience
    from memoryking.adapters.handoff import Handoff
    from memoryking.adapters.inference import Inference
    from memoryking.adapters.inspection import Inspection
    from memoryking.adapters.knowledge_store import Knowledge
    from memoryking.adapters.repository_store import Repository
    from memoryking.adapters.retrieval import Retrieval
    from memoryking.adapters.working import Working

    if (
        set(body) != {"csrf", "operation", "command"}
        or not isinstance(body["csrf"], str)
        or not secrets.compare_digest(body["csrf"], session["csrf"])
    ):
        raise LocalError("csrf_rejected")
    if len(canonical_bytes(body)) > 131072 or not isinstance(body["command"], dict):
        raise LocalError("invalid_request")
    operation, command = body["operation"], body["command"]
    allowed = {
        "retrieval": {"atlas", "fetch", "replay", "brief", "rerun"},
        "knowledge": {"query", "constraints", "propose", "review", "attest"},
        "experience": {
            "query",
            "records",
            "record",
            "review",
            "activate",
            "suspend",
            "ranking_inspect",
        },
        "repository": {"impact", "history", "coverage"},
        "working": {"resume", "operations"},
        "handoff": {"prepare", "preview", "materialize", "cancel"},
        "inspection": {
            "history",
            "operations",
            "cost",
            "handoffs",
            "retention_preview",
            "purge",
            "influence",
        },
        "inference": {"audit"},
    }
    if (
        not isinstance(operation, str)
        or operation not in allowed
        or not isinstance(command.get("action"), str)
        or command["action"] not in allowed[operation]
    ):
        raise LocalError("dashboard_action_unsupported")
    services: dict[str, Callable[[StoreCore], CommandService]] = {
        "retrieval": Retrieval,
        "knowledge": Knowledge,
        "experience": Experience,
        "repository": Repository,
        "working": Working,
        "handoff": Handoff,
        "inspection": Inspection,
        "inference": Inference,
    }
    guarded = cast(StoreCore, BrowserStore(store, session, check_session))
    return services[operation](guarded).execute(session["credential"], command)
