"""Capture from scripts, test runners and editor hooks without writing a client.

Connection details come from `--profile`-style arguments or the environment:
`MEMORYKING_PROFILE`, or `MEMORYKING_SOCKET` with `MEMORYKING_CREDENTIAL`, plus
`MEMORYKING_PROJECT`. Every capture goes through the ordinary authorized path with a
deterministic event id, so re-running a hook replays instead of duplicating.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any

from memoryking import profiles, session_bindings
from memoryking.adapters.local_files import private_file
from memoryking.adapters.local_ipc import request
from memoryking.domain.local import LocalError
from memoryking.relay_client import RelayCredential, credential_token

TEXT_LIMIT = 65536
DAY_MS = 86_400_000


def connection_files(
    *,
    profile: str | None = None,
    socket: Path | None = None,
    credential: Path | None = None,
    project: str | None = None,
    config: Path | None = None,
) -> tuple[Path, Path, str]:
    """(socket, credential file, project) from arguments, then the environment."""
    name = profile or os.environ.get("MEMORYKING_PROFILE")
    if name and (socket is None or credential is None):
        resolved = profiles.resolve(name, config)
        socket = socket or Path(resolved["socket"])
        credential = credential or Path(resolved["credential"])
    socket = socket or (
        Path(os.environ["MEMORYKING_SOCKET"]) if os.environ.get("MEMORYKING_SOCKET") else None
    )
    credential = credential or (
        Path(os.environ["MEMORYKING_CREDENTIAL"])
        if os.environ.get("MEMORYKING_CREDENTIAL")
        else None
    )
    project = project or os.environ.get("MEMORYKING_PROJECT")
    if socket is None:
        raise LocalError("socket_required")
    if credential is None:
        raise LocalError("credential_required")
    if not project:
        raise LocalError("project_required")
    if not isinstance(credential, RelayCredential):
        private_file(credential)
    return socket, credential, project


def connection(**where: Any) -> tuple[Path, str, str]:
    """(socket, credential token, project) from arguments, then the environment."""
    socket, credential, project = connection_files(**where)
    return socket, credential_token(credential), project


def event_id(source: str, text: str, salt: str = "") -> str:
    return "ev-" + hashlib.sha256(f"{source}\n{salt}\n{text}".encode()).hexdigest()[:40]


def capture(
    text: str,
    *,
    source: str = "hook",
    event: str | None = None,
    readers: list[str] | None = None,
    classification: str = "internal",
    observed_ms: int | None = None,
    namespace: str = "hooks",
    supersedes: str | None = None,
    scope: str = "project",
    **where: Any,
) -> dict[str, Any]:
    """Capture one observation; the text is truncated to the capture limit with a marker.

    With `scope="user"` it goes to the user scope the credential acts for instead of the
    project: durable facts and preferences about the user, shared by all their projects.

    A replayed capture must send identical bytes, so the default observed time is the
    current day rather than the millisecond; the store's event log records the exact
    moment of the capture itself. Pass `observed_ms` to keep a precise time.
    """
    socket, token, project = connection(**where)
    if scope == "user":
        project = request(socket, token, "user", {"action": "scope"})["project"]
    elif scope != "project":
        raise LocalError("invalid_request")
    # Unpaired surrogates (e.g. from a decoded traceback) become replacement characters.
    text = text.encode("utf-8", "replace").decode("utf-8")
    if len(text.encode()) > TEXT_LIMIT:
        text = text.encode()[: TEXT_LIMIT - 32].decode("utf-8", "ignore") + "\n[truncated]"
    if not text.strip():
        raise LocalError("invalid_request")
    observed = (
        observed_ms if observed_ms is not None else int(time.time() * 1000) // DAY_MS * DAY_MS
    )
    # The derived key covers everything the payload varies by (project, observed time,
    # text), so a rerun replays and another day or project is a new capture.
    event = event or event_id(source, text, f"{project}\n{observed}")
    body: dict[str, Any] = {
        "version": 1,
        "project": project,
        "source": source,
        "source_event": event,
        "text": text,
        "readers": readers,
        "classification": classification,
        "observed_at_ms": observed,
    }
    if supersedes:
        body["supersedes"] = supersedes
    space = request(socket, token, "namespace", {"project": project, "name": namespace})
    return request(
        socket,
        token,
        "capture",
        {
            "namespace": space["namespace"],
            "key": event,
            "request": body,
        },
    )


def as_of_ms(value: str) -> int:
    """A wall-clock instant for `--as-of`: Unix ms, a date, or an ISO date-time (UTC default)."""
    from datetime import datetime, timezone

    text = value.strip()
    if text.isdigit():
        return int(text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise LocalError("invalid_request") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if len(text) == 10:
        # A bare date means the end of that day: everything recorded on it counts.
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999000)
    return int(parsed.timestamp() * 1000)


def range_ms(value: str, *, end: bool) -> int:
    """An end of a `--from`/`--to` range: Unix ms, a date, or an ISO date-time (UTC default).

    A bare date is the start of that day for `--from` and the end of it for `--to`, so
    `--from 2026-03-01 --to 2026-03-31` covers all of March."""
    from datetime import datetime, timezone

    if value.strip().isdigit() or end or len(value.strip()) != 10:
        return as_of_ms(value)
    try:
        parsed = datetime.fromisoformat(value.strip()).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise LocalError("invalid_request") from exc
    return int(parsed.timestamp() * 1000)


def start(
    *,
    budget: int = 6000,
    text: bool = True,
    task: str | None = None,
    days: int | None = None,
    binding: list[str] | None = None,
    **where: Any,
) -> dict[str, Any]:
    """The query-free session-start briefing: constraints, pins, the task's last checkpoint
    and recent decisions (see `adapters/session_start.py`), as Markdown by default."""
    socket, token, project = connection(**where)
    points = session_bindings.resolve(
        binding, profile=where.get("profile"), config=where.get("config")
    )
    context = request(socket, token, "context", {"project": project})
    args: dict[str, Any] = {
        "project": project,
        "bindings": points,
        "max_output_tokens": budget,
        "tokenizer": "chars-estimate-v1",
    }
    if text:
        args["format"] = "text"
    if task:
        args["task"] = task
    if days:
        args["recent_days"] = days
    command = {
        "version": 1,
        "incarnation": context["incarnation"],
        "action": "start",
        "args": args,
    }
    return request(socket, token, "retrieval", {"command": command})


def recall(
    query: str,
    *,
    budget: int = 6000,
    text: bool = True,
    task: str | None = None,
    as_of: int | None = None,
    salience: bool = False,
    rerank: bool = False,
    binding: list[str] | None = None,
    time_range: dict[str, int] | None = None,
    limit: int | None = None,
    min_score_ppm: int | None = None,
    **where: Any,
) -> dict[str, Any]:
    """A briefing through the socket, as cited Markdown by default; `as_of` in Unix ms.

    `limit` caps the items (default: as many distinct items as fit `budget`, up to 32) and
    `min_score_ppm` leaves out guidance that matches the query less well than that.

    `time_range` (`from_ms` and/or `to_ms`) restricts the search to records observed in it.

    `binding` is a list of `key=value` strings; with none, `MEMORYKING_BINDINGS` and the
    named profile's `bindings` apply (see `session_bindings`)."""
    socket, token, project = connection(**where)
    points = session_bindings.resolve(
        binding, profile=where.get("profile"), config=where.get("config")
    )
    context = request(socket, token, "context", {"project": project})
    args: dict[str, Any] = {
        "project": project,
        "query": query,
        "bindings": points,
        "max_output_tokens": budget,
        "tokenizer": "chars-estimate-v1",
    }
    if text:
        args["format"] = "text"
    if task:
        args["task"] = task
    if as_of is not None:
        args["as_of_ms"] = as_of
    if salience:
        args["salience"] = "on"
    if rerank:
        args["rerank"] = "on"
    if time_range:
        args["time_range"] = time_range
    if limit is not None:
        args["top_k"] = limit
    if min_score_ppm:
        args["min_score_ppm"] = min_score_ppm
    command = {
        "version": 1,
        "incarnation": context["incarnation"],
        "action": "brief",
        "args": args,
    }
    return request(socket, token, "retrieval", {"command": command})
