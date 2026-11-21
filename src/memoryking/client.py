"""Connect in one call, and use the same client from asyncio.

`MemoryKing.connect()` reads the same profile and `MEMORYKING_*` variables the command line
honours, so a program running next to a configured store needs no paths. The async clients
run the synchronous client on a worker thread (see `AsyncClient`).
"""

from __future__ import annotations

import asyncio
import os
from datetime import date, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

from memoryking import profiles, session_bindings
from memoryking.domain.local import LocalError
from memoryking.memory_api import (
    Briefing,
    Classification,
    FailedAttempt,
    HandOff,
    Progress,
    Remembered,
    Resumed,
    Role,
)
from memoryking.sdk import LocalClient
from memoryking.shared_sdk import SharedClient


def _env(name: str) -> str | None:
    return os.environ.get(name) or None


def connect(
    *,
    profile: str | None = None,
    socket: Path | None = None,
    credential: Path | None = None,
    project: str | None = None,
    config: Path | None = None,
    url: str | None = None,
    tenant: str | None = None,
    ca_file: Path | None = None,
    loopback_http: bool = False,
    namespace: str = "sdk",
) -> LocalClient:
    """A bound client from arguments, then the environment.

    Local store: `profile` (`MEMORYKING_PROFILE`) or `socket` and `credential`
    (`MEMORYKING_SOCKET`, `MEMORYKING_CREDENTIAL`), and `project` (`MEMORYKING_PROJECT`).
    Session bindings come from `MEMORYKING_BINDINGS` and the profile. Shared service: pass
    `url` (`MEMORYKING_URL`), `tenant` (`MEMORYKING_TENANT`), `credential`, `project` and
    optionally `ca_file` (`MEMORYKING_CA_FILE`) for a private certificate authority.
    """
    url = url or _env("MEMORYKING_URL")
    project = project or _env("MEMORYKING_PROJECT")
    name = profile or _env("MEMORYKING_PROFILE")
    if url is not None:
        credential = credential or (Path(c) if (c := _env("MEMORYKING_CREDENTIAL")) else None)
        tenant = tenant or _env("MEMORYKING_TENANT")
        ca = ca_file or (Path(c) if (c := _env("MEMORYKING_CA_FILE")) else None)
        if credential is None:
            raise LocalError("credential_required")
        if not tenant:
            raise LocalError("tenant_required")
        if not project:
            raise LocalError("project_required")
        return SharedClient(
            url,
            tenant,
            credential,
            project,
            ca_file=ca,
            loopback_http=loopback_http,
            namespace=namespace,
            bindings=session_bindings.resolve(profile=name, config=config),
        )
    if name and (socket is None or credential is None):
        resolved = profiles.resolve(name, config)
        socket = socket or Path(resolved["socket"])
        credential = credential or Path(resolved["credential"])
    socket = socket or (Path(s) if (s := _env("MEMORYKING_SOCKET")) else None)
    credential = credential or (Path(c) if (c := _env("MEMORYKING_CREDENTIAL")) else None)
    if socket is None:
        raise LocalError("socket_required")
    if credential is None:
        raise LocalError("credential_required")
    if not project:
        raise LocalError("project_required")
    bindings = session_bindings.resolve(profile=name, config=config)
    return LocalClient(socket, credential, project, namespace=namespace, bindings=bindings)


class MemoryKing:
    """Entry point: `MemoryKing.connect()` for a sync client, `await MemoryKing.aconnect()`."""

    @staticmethod
    def connect(**options: Any) -> LocalClient:
        """See `memoryking.client.connect`; returns a `LocalClient` or `SharedClient`."""
        return connect(**options)

    @staticmethod
    async def aconnect(**options: Any) -> AsyncClient:
        """The async twin: `async with await MemoryKing.aconnect() as memory:`."""
        return AsyncClient(await asyncio.to_thread(connect, **options))


class AsyncClient:
    """The sync client's everyday surface for asyncio, run on worker threads.

    The standard library has no async HTTPS client, and the client's current-context
    checks, defaults, idempotency keys and checkpoint merging live in one synchronous code
    path that the sockets and HTTP transport share; reimplementing it over asyncio streams
    would duplicate security-relevant logic. Each awaited call therefore runs the sync
    method with `asyncio.to_thread`: the event loop never blocks and calls from different
    tasks run concurrently. Cancelling an awaiting task does not cancel a request already
    sent, so a cancelled write may still have been applied; retry it with the same `event`
    or task state rather than assuming it did not happen.
    """

    def __init__(self, client: LocalClient):
        self.client = client

    async def __aenter__(self) -> AsyncClient:
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await asyncio.to_thread(self.client.close)

    @property
    def project(self) -> str:
        return self.client.project

    async def context(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.client.context)

    async def remember(
        self,
        text: str,
        *,
        source: str = "agent",
        event: str | None = None,
        readers: list[str] | None = None,
        classification: Classification = "internal",
        observed_ms: int | None = None,
    ) -> Remembered:
        return await asyncio.to_thread(
            lambda: self.client.remember(
                text,
                source=source,
                event=event,
                readers=readers,
                classification=classification,
                observed_ms=observed_ms,
            )
        )

    async def recall(
        self,
        query: str,
        *,
        budget: int = 6000,
        as_of: str | datetime | date | None = None,
        rerank: bool = False,
        salience: bool = False,
        task: str | None = None,
        format: Literal["text", "json"] = "text",
    ) -> Briefing:
        return await asyncio.to_thread(
            lambda: self.client.recall(
                query,
                budget=budget,
                as_of=as_of,
                rerank=rerank,
                salience=salience,
                task=task,
                format=format,
            )
        )

    async def save_progress(
        self,
        task: str,
        objective: str,
        current_state: str,
        *,
        next_actions: list[str] | None = None,
        findings: list[str] | None = None,
        failed_attempts: list[FailedAttempt] | None = None,
        blockers: list[str] | None = None,
        hard_constraints: list[str] | None = None,
        run: str | None = None,
        share_with: list[str] | None = None,
    ) -> Progress:
        return await asyncio.to_thread(
            lambda: self.client.save_progress(
                task,
                objective,
                current_state,
                next_actions=next_actions,
                findings=findings,
                failed_attempts=failed_attempts,
                blockers=blockers,
                hard_constraints=hard_constraints,
                run=run,
                share_with=share_with,
            )
        )

    async def resume(self, task: str, *, run: str | None = None, budget: int = 12000) -> Resumed:
        return await asyncio.to_thread(lambda: self.client.resume(task, run=run, budget=budget))

    async def hand_off(
        self,
        task: str,
        to: str,
        *,
        role: Role = "review",
        stage: str = "handoff",
        ttl_minutes: int = 60,
        run: str | None = None,
    ) -> HandOff:
        return await asyncio.to_thread(
            lambda: self.client.hand_off(
                task, to, role=role, stage=stage, ttl_minutes=ttl_minutes, run=run
            )
        )


class AsyncLocalClient(AsyncClient):
    """Async client for the local daemon's Unix socket."""

    @classmethod
    async def connect(
        cls,
        socket: Path,
        credential_file: Path,
        project: str,
        namespace: str = "sdk",
        bindings: list[dict[str, Any]] | None = None,
    ) -> AsyncLocalClient:
        client = await asyncio.to_thread(
            LocalClient, socket, credential_file, project, namespace, bindings
        )
        return cls(client)


class AsyncSharedClient(AsyncClient):
    """Async client for the shared HTTPS service."""

    @classmethod
    async def connect(
        cls,
        url: str,
        tenant: str,
        credential_file: Path,
        project: str,
        *,
        ca_file: Path | None = None,
        loopback_http: bool = False,
        namespace: str = "sdk",
        bindings: list[dict[str, Any]] | None = None,
    ) -> AsyncSharedClient:
        client = await asyncio.to_thread(
            lambda: SharedClient(
                url,
                tenant,
                credential_file,
                project,
                ca_file=ca_file,
                loopback_http=loopback_http,
                namespace=namespace,
                bindings=bindings,
            )
        )
        return cls(client)
