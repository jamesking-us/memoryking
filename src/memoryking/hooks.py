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

from memoryking import profiles
from memoryking.adapters.local_files import private_file
from memoryking.adapters.local_ipc import request
from memoryking.domain.local import LocalError

TEXT_LIMIT = 65536
DAY_MS = 86_400_000


def connection(
    *,
    profile: str | None = None,
    socket: Path | None = None,
    credential: Path | None = None,
    project: str | None = None,
    config: Path | None = None,
) -> tuple[Path, str, str]:
    """(socket, credential token, project) from arguments, then the environment."""
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
    private_file(credential)
    return socket, credential.read_text().strip(), project


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
    **where: Any,
) -> dict[str, Any]:
    """Capture one observation; the text is truncated to the capture limit with a marker.

    A replayed capture must send identical bytes, so the default observed time is the
    current day rather than the millisecond; the store's event log records the exact
    moment of the capture itself. Pass `observed_ms` to keep a precise time.
    """
    socket, token, project = connection(**where)
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
    space = request(socket, token, "namespace", {"project": project, "name": namespace})
    return request(
        socket,
        token,
        "capture",
        {
            "namespace": space["namespace"],
            "key": event,
            "request": {
                "version": 1,
                "project": project,
                "source": source,
                "source_event": event,
                "text": text,
                "readers": readers,
                "classification": classification,
                "observed_at_ms": observed,
            },
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


def recall(
    query: str,
    *,
    budget: int = 6000,
    text: bool = True,
    task: str | None = None,
    as_of: int | None = None,
    salience: bool = False,
    **where: Any,
) -> dict[str, Any]:
    """A briefing through the socket, as cited Markdown by default; `as_of` in Unix ms."""
    socket, token, project = connection(**where)
    context = request(socket, token, "context", {"project": project})
    args: dict[str, Any] = {
        "project": project,
        "query": query,
        "bindings": [{}],
        "max_output_tokens": budget,
        "tokenizer": "utf8-byte-estimate-v1",
    }
    if text:
        args["format"] = "text"
    if task:
        args["task"] = task
    if as_of is not None:
        args["as_of_ms"] = as_of
    if salience:
        args["salience"] = "on"
    command = {
        "version": 1,
        "incarnation": context["incarnation"],
        "action": "brief",
        "args": args,
    }
    return request(socket, token, "retrieval", {"command": command})
