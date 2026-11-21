"""Host hooks: memory that loads itself at session start and saves itself before loss.

Claude Code, Codex and Grok all run command hooks at the same three moments: when a
session starts or resumes (including right after compaction), before the transcript is
compacted, and when the session ends. Each hook receives one JSON object on stdin and
MemoryKing turns it into an ordinary authorized call:

- `session-start` prints a cited Markdown briefing (and, when a task is named, the
  agent's own latest checkpoint for it plus the hooks' checkpoint). Claude Code and Codex add a session-start hook's stdout to the
  model's context; Grok ignores hook stdout, so there the briefing goes nowhere unless
  the host's own instructions read it from a file.
- `pre-compact` captures the tail of the transcript as an observation before the host
  compresses it, and saves a minimal checkpoint when a task is named.
- `session-end` captures the transcript tail (or the host's last assistant message)
  so the final state survives the session.

The tail holds user turns, assistant text and tool errors; tool-call inputs and
successful tool results are never read. Secret-shaped values are replaced before
anything is stored (a best-effort safety net, see `scrub`). Hook checkpoints go to their
own stream, `<task>--mkhook`, so they never replace a checkpoint the agent wrote.

A hook never fails the host: every error is reported on stderr and the exit code stays
zero. Captures use deterministic event ids, so a hook that fires twice replays.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from memoryking import hooks, session_bindings
from memoryking.adapters.local_ipc import request
from memoryking.domain.checkpoint import complete
from memoryking.domain.local import LocalError

HOSTS = ("claude", "codex", "grok")
KINDS = ("session-start", "pre-compact", "session-end")
TAIL_BYTES = 262_144
TAIL_CHARS = 16_384
STATE_CHARS = 2_000
HOOK_SUFFIX = "--mkhook"
REDACTED = "[REDACTED]"
# Tool-call shapes whose payload is an input (a file body, a command), never read.
TOOL_CALLS = {
    "tool_use",
    "server_tool_use",
    "function_call",
    "tool_call",
    "custom_tool_call",
    "local_shell_call",
    "mcp_tool_call",
}
TOOL_RESULTS = {"tool_result", "function_call_output", "custom_tool_call_output"}
SKIPPED = {"thinking", "redacted_thinking", "reasoning"}
SECRET_PATTERNS = (
    re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY"
        r"(?: BLOCK)?-----|\Z)",
        re.S,
    ),
    re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[A-Z0-9]{16}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"),
)
# name = value / name: value / "name": "value"; the name stays, the value goes.
SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b[\w.-]*(?:password|passwd|secret|api[_-]?key|access[_-]?key|private[_-]?key"
    r"|(?:auth|access|refresh|api|session)[_-]?token)[\w.-]*[\"']?\s*[:=]\s*[\"']?)"
    r"([^\s\"'`,;&]{4,})"
)
EVENT_KINDS = {
    "SessionStart": "session-start",
    "PreCompact": "pre-compact",
    "SessionEnd": "session-end",
    "Stop": "session-end",
}


def read_event(raw: str | bytes, environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Normalize a host's hook payload: snake_case (Claude Code, Codex) or camelCase (Grok).

    Missing stdin is an empty event; the environment supplies what Grok exports."""
    env = os.environ if environ is None else environ
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    data: dict[str, Any] = {}
    if text.strip():
        try:
            loaded = json.loads(text)
        except ValueError as exc:
            raise LocalError("hook_event_invalid") from exc
        if not isinstance(loaded, dict):
            raise LocalError("hook_event_invalid")
        data = loaded

    def pick(*names: str) -> Any:
        for name in names:
            value = data.get(name)
            if value is not None:
                return value
        return None

    host = (
        "grok"
        if "hookEventName" in data or "GROK_HOOK_EVENT" in env
        else "claude"
        if "scratchpad_dir" in data or "prompt_id" in data or "CLAUDE_PROJECT_DIR" in env
        else "codex"
        if "turn_id" in data or "model" in data
        else None
    )
    name = pick("hook_event_name", "hookEventName") or env.get("GROK_HOOK_EVENT")
    return {
        "host": host,
        "event": name,
        "kind": EVENT_KINDS.get(str(name), None),
        "session": str(pick("session_id", "sessionId") or env.get("GROK_SESSION_ID") or ""),
        "cwd": str(
            pick("cwd")
            or env.get("GROK_WORKSPACE_ROOT")
            or env.get("CLAUDE_PROJECT_DIR")
            or os.getcwd()
        ),
        "transcript": pick("transcript_path", "transcriptPath"),
        "source": pick("source"),
        "trigger": pick("trigger"),
        "instructions": pick("custom_instructions"),
        "last_message": pick("last_assistant_message"),
    }


def scrub(text: str) -> tuple[str, int]:
    """Replace common secret shapes with a fixed marker; returns (text, replacements).

    Covers private-key blocks, `sk-`/`rk-` and Stripe-style keys, AWS access key ids,
    GitHub, Slack and Google API tokens, JWTs, bearer tokens, and the value in
    `password=`, `secret:`, `api_key=`, `access_key=`, `*_token=` style assignments.
    It is a safety net for what a transcript might carry, not a guarantee: a secret in
    an unrecognized shape, split across lines, encoded, or written in prose passes
    through, so keep secrets out of transcripts in the first place."""
    count = 0
    for pattern in SECRET_PATTERNS:
        text, n = pattern.subn(REDACTED, text)
        count += n
    # A value that is already the marker (replaced above) is not a new find.
    text, n = SECRET_ASSIGNMENT.subn(
        lambda m: m.group(1) + (m.group(2) if m.group(2).startswith(REDACTED) else REDACTED), text
    )
    return text, count + n


def _flat(value: Any) -> str:
    """The plain text inside a content value, never descending into tool inputs."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(p for p in (_flat(v) for v in value) if p.strip())
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return str(value["text"])
    return ""


def _texts(value: Any, out: list[str], role: str | None = None) -> None:
    """Collect user turns, assistant text and tool errors from any transcript line shape.

    Tool-call inputs (file bodies, commands) and successful tool results are skipped."""
    if isinstance(value, dict):
        kind = value.get("type")
        if kind in TOOL_CALLS or kind in SKIPPED:
            return
        if kind in TOOL_RESULTS:
            if value.get("is_error") is True:
                message = _flat(value.get("content", value.get("output")))
                if message.strip():
                    out.append(f"Tool error: {message}")
            return
        named = value.get("role") or kind
        if named in ("user", "assistant"):
            role = named
        elif named in ("tool", "system", "developer"):
            role = None
        for key, item in value.items():
            if key in ("input", "arguments", "parameters"):
                continue
            if key in ("text", "content") and isinstance(item, str):
                if role and item.strip():
                    out.append(f"{role.capitalize()}: {item}")
            else:
                _texts(item, out, role)
    elif isinstance(value, list):
        for item in value:
            _texts(item, out, role)


def _tail(path: str | Path | None, limit: int = TAIL_CHARS) -> tuple[str, int]:
    """(scrubbed tail, number of secret-shaped values replaced); see `transcript_tail`."""
    if not path:
        return "", 0
    try:
        file = Path(path)
        size = file.stat().st_size
        with file.open("rb") as handle:
            if size > TAIL_BYTES:
                handle.seek(size - TAIL_BYTES)
                handle.readline()  # drop the torn first line
            raw = handle.read(TAIL_BYTES).decode("utf-8", "replace")
    except OSError:
        return "", 0
    pieces: list[str] = []
    parsed = False
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        parsed = True
        _texts(value, pieces)
    # Scrub before slicing so a cut cannot leave half a secret at the start.
    text, count = scrub("\n\n".join(pieces) if parsed else raw)
    return text[-limit:].strip(), count


def transcript_tail(path: str | Path | None, limit: int = TAIL_CHARS) -> str:
    """The newest user turns, assistant text and tool errors of a JSONL transcript, or the
    raw tail of any text file, with secret-shaped values replaced (see `scrub`).

    Reads at most the last 256 KiB and returns at most `limit` characters; a missing
    or unreadable transcript is an empty string, never an error."""
    return _tail(path, limit)[0]


def hook_task(task: str) -> str:
    """The stream hooks write to, kept apart from the agent's own checkpoints on `task`."""
    if len(task) + len(HOOK_SUFFIX) <= 96:
        return task + HOOK_SUFFIX
    return task[:75] + "-" + hashlib.sha256(task.encode()).hexdigest()[:12] + HOOK_SUFFIX


def session_start(
    event: dict[str, Any],
    *,
    query: str | None = None,
    task: str | None = None,
    budget: int = 6000,
    binding: list[str] | None = None,
    **where: Any,
) -> str:
    """Cited Markdown for the model: the task's resume packet first, then a briefing."""
    socket, token, project = hooks.connection(**where)
    points = session_bindings.resolve(
        binding, profile=where.get("profile"), config=where.get("config")
    )
    parts: list[str] = []
    if task:
        context = request(socket, token, "context", {"project": project})
        # The agent's own latest checkpoint first, then the hooks' (a separate stream).
        for stream, share in ((task, budget), (hook_task(task), budget // 2)):
            try:
                packet = request(
                    socket,
                    token,
                    "working",
                    {
                        "command": {
                            "version": 1,
                            "incarnation": context["incarnation"],
                            "action": "resume",
                            "args": {
                                "project": project,
                                "task": stream,
                                "run": None,
                                "checkpoint": None,
                                "bindings": points,
                                "max_output_tokens": share,
                                "tokenizer": "utf8-byte-estimate-v1",
                                "format": "text",
                            },
                        }
                    },
                )
                parts.append(str(packet.get("text", "")).rstrip())
            except LocalError as exc:
                if exc.code != "checkpoint_unavailable":
                    raise
    packet = hooks.recall(
        query or task or project,
        budget=budget,
        text=True,
        task=task,
        binding=binding,
        socket=socket,
        credential=Path(where["credential"]) if where.get("credential") else None,
        project=project,
        profile=where.get("profile"),
        config=where.get("config"),
    )
    parts.append(str(packet.get("text", "")).rstrip())
    return "\n\n".join(p for p in parts if p) + "\n"


def _save_state(
    socket: Path,
    token: str,
    project: str,
    task: str,
    session: str,
    kind: str,
    text: str,
    observation: str | None,
) -> dict[str, Any]:
    context = request(socket, token, "context", {"project": project})
    # Day-granular like hook captures: a hook that fires twice sends identical bytes
    # under its stable retry key and replays instead of conflicting.
    observed = int(time.time() * 1000) // hooks.DAY_MS * hooks.DAY_MS
    # The newest part of the tail is the live state; the key names the hook so a
    # pre-compact and a session-end over the same tail are two saves, not a conflict.
    state = text[-STATE_CHARS:].strip()
    if len(text) > STATE_CHARS:
        state = "[earlier text omitted] " + state
    body = complete(
        {
            "objective": f"Session {session[:12] or 'unknown'} on {project} (hook capture)",
            "current_state": state,
            "references": [{"kind": "observation", "id": observation}] if observation else [],
        },
        observed,
    )
    return request(
        socket,
        token,
        "working",
        {
            "command": {
                "version": 1,
                "incarnation": context["incarnation"],
                "action": "save",
                "key": hooks.event_id(
                    f"hook-checkpoint-{kind}", text, f"{project}\n{task}\n{session}"
                ),
                "args": {
                    "project": project,
                    "task": hook_task(task),
                    "run": None,
                    "readers": None,
                    "expected_revision": None,
                    "checkpoint": body,
                },
            }
        },
    )


def save(
    event: dict[str, Any],
    kind: str,
    *,
    host: str | None = None,
    task: str | None = None,
    **where: Any,
) -> dict[str, Any]:
    """Capture what the host is about to lose; with a task, also save a checkpoint.

    `pre-compact` captures the transcript tail (plus the user's compaction instructions
    when the host passes them); `session-end` captures the host's last assistant message
    when given, else the transcript tail. The event id covers host, session and text, so
    the same moment replays and a later compaction is a new observation."""
    socket, token, project = hooks.connection(**where)
    name = host or event.get("host") or "host"
    redacted = 0
    if kind == "session-end" and event.get("last_message"):
        text, redacted = scrub(str(event["last_message"]))
    else:
        text, redacted = _tail(event.get("transcript"))
        if kind == "pre-compact" and event.get("instructions"):
            note, more = scrub(str(event["instructions"]))
            text = f"Compaction instructions: {note}\n\n{text}".strip()
            redacted += more
    if not text.strip():
        return {"captured": False, "reason": "nothing_to_capture"}
    if redacted:
        text += f"\n\n[memoryking: {redacted} secret-shaped value(s) redacted before capture]"
    source = f"{name}-{'compact' if kind == 'pre-compact' else 'session'}"
    salt = f"{event.get('session', '')}\n{event.get('trigger') or event.get('source') or ''}"
    result = hooks.capture(
        text,
        source=source,
        event=hooks.event_id(source, text, salt),
        socket=socket,
        credential=Path(where["credential"]) if where.get("credential") else None,
        project=project,
        profile=where.get("profile"),
        config=where.get("config"),
    )
    summary: dict[str, Any] = {
        "captured": True,
        "observation": result.get("observation"),
        "source": source,
        "redacted": redacted,
    }
    if task:
        saved = _save_state(
            socket,
            token,
            project,
            task,
            str(event.get("session", "")),
            kind,
            text,
            result.get("observation"),
        )
        summary["checkpoint"] = saved.get("checkpoint")
        summary["revision"] = saved.get("revision")
    return summary


# --- Installation -------------------------------------------------------------------

MARK = "memoryking"


def hook_command(
    kind: str,
    *,
    host: str,
    project: str,
    profile: str | None,
    socket: Path | None,
    credential: Path | None,
    task: str | None,
    query: str | None,
    bindings: dict[str, str] | None = None,
) -> str:
    """The shell command a host runs; absolute paths so it works without the shell env."""
    parts = [json.dumps(sys.executable), "-m", "memoryking"]
    if profile:
        parts += ["--profile", profile]
    parts += ["hook", kind, "--host", host, "--project", project]
    if socket is not None:
        parts += ["--socket", json.dumps(str(socket))]
    if credential is not None:
        parts += ["--credential", json.dumps(str(credential))]
    if task:
        parts += ["--task", task]
    if query and kind == "session-start":
        parts += ["--query", json.dumps(query)]
    for key, value in sorted((bindings or {}).items()):
        parts += ["--binding", json.dumps(f"{key}={value}")]
    return " ".join(parts)


def config_path(host: str, scope: str, cwd: Path, home: Path) -> Path:
    if host not in HOSTS:
        raise LocalError("host_unsupported")
    if scope not in ("project", "user"):
        raise LocalError("invalid_request")
    base = cwd if scope == "project" else home
    return {
        "claude": base / ".claude/settings.json",
        "codex": base / ".codex/hooks.json",
        "grok": base / ".grok/hooks/memoryking.json",
    }[host]


def _entries(host: str, commands: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    """Hook entries per event, in the shape all three hosts read."""
    timeout = {"claude": 30, "codex": 30, "grok": 20}[host]
    status = {"session-start": "Loading MemoryKing briefing", "pre-compact": "Saving to MemoryKing"}

    def entry(kind: str, matcher: str | None, seconds: int) -> dict[str, Any]:
        hook: dict[str, Any] = {"type": "command", "command": commands[kind], "timeout": seconds}
        if host == "codex" and kind in status:
            hook["statusMessage"] = status[kind]
        value: dict[str, Any] = {"hooks": [hook]}
        if matcher:
            value["matcher"] = matcher
        return value

    return {
        "SessionStart": [entry("session-start", "startup|resume|compact", timeout)],
        "PreCompact": [entry("pre-compact", None, timeout)],
        "SessionEnd": [entry("session-end", None, 10 if host == "claude" else timeout)],
    }


def _ours(group: dict[str, Any]) -> bool:
    return any(
        isinstance(h, dict)
        and MARK in str(h.get("command", ""))
        and " hook " in str(h.get("command", ""))
        for h in group.get("hooks", [])
    )


def merge(
    existing: dict[str, Any], entries: dict[str, list[dict[str, Any]]], *, remove: bool
) -> dict[str, Any]:
    """Add or replace MemoryKing's entries, leaving every other hook untouched."""
    value = dict(existing)
    section = dict(value.get("hooks") or {})
    for event, ours in entries.items():
        groups = [g for g in section.get(event, []) if isinstance(g, dict) and not _ours(g)]
        if not remove:
            groups.extend(ours)
        if groups:
            section[event] = groups
        else:
            section.pop(event, None)
    if section:
        value["hooks"] = section
    else:
        value.pop("hooks", None)
    return value


def install(
    *,
    host: str,
    scope: str,
    project: str,
    cwd: Path,
    home: Path,
    profile: str | None = None,
    socket: Path | None = None,
    credential: Path | None = None,
    task: str | None = None,
    query: str | None = None,
    binding: list[str] | None = None,
    remove: bool = False,
    write: bool = True,
) -> dict[str, Any]:
    """Merge MemoryKing's three hooks into the host's config file (or show the result).

    `binding` (`key=value` strings) is written into each command, so the hooks resolve
    constraints as the MCP bridge does; a profile's or the environment's bindings still apply."""
    path = config_path(host, scope, cwd, home)
    bindings = session_bindings.parse_pairs(binding or [])
    commands = {
        kind: hook_command(
            kind,
            host=host,
            project=project,
            profile=profile,
            socket=socket,
            credential=credential,
            task=task,
            query=query,
            bindings=bindings,
        )
        for kind in KINDS
    }
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text())
        except ValueError as exc:
            raise LocalError("hook_config_invalid") from exc
        if not isinstance(loaded, dict):
            raise LocalError("hook_config_invalid")
        existing = loaded
    merged = merge(existing, _entries(host, commands), remove=remove)
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(merged, indent=2) + "\n")
    return {
        "host": host,
        "path": str(path),
        "written": write,
        "removed": remove,
        "events": sorted(_entries(host, commands)) if not remove else [],
        "config": merged,
        "note": "Grok ignores hook stdout, so the session-start briefing is not injected there."
        if host == "grok"
        else None,
    }
