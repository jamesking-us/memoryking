"""A seeded session as the host would have lived it: the transcript file and the hook events.

Products that hook into Claude Code (claude-mem, MCP Memory Service, Basic Memory, MemoryKing)
see a session only through what the host hands its hooks. This module turns a recorded session
(`Session`: user, assistant and tool turns) into exactly that, read from the host's own schema
(Claude Code 2.1.330, `claude` binary, hook input types):

* every hook gets `session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`;
* `UserPromptSubmit` carries `prompt`, once per user turn;
* `PostToolUse` carries `tool_name`, `tool_input`, `tool_response`, `tool_use_id`; a tool call
  that failed is `PostToolUseFailure` with `error` instead of a response;
* `Stop` fires once when the assistant ends the turn that a user prompt started, not for text it
  wrote before a tool call, and carries `last_assistant_message`, the text of that last message
  ("avoids the need to read and parse the transcript file", the host's own words);
* `SessionEnd` fires once at the end with a `reason`.

The transcript file is Claude Code's JSONL: one line per user prompt, per assistant text block,
per tool call and per tool result, and it holds the lines written so far when each hook fires.
A recorded tool turn carries no tool name or input, so both are inferred from its text (a test
run, a diff, a search, a failed page fetch; anything else is a `Bash` call): a stated limit of
seeding, because a real session knows what it ran.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .base import Session, Turn

MODEL = "claude-sonnet-5-5"
URL = re.compile(r"https?://[^\s)\"'>]+")


@dataclass
class HookEvent:
    name: str  # the host's hook_event_name
    payload: dict
    transcript: list[dict] = field(default_factory=list)  # the transcript lines so far


def tool_call(text: str) -> tuple[str, dict]:
    """(tool name, tool input) that a recorded tool turn most plausibly was."""
    body = text[len("ERROR: ") :] if text.startswith("ERROR: ") else text
    url = URL.search(body)
    if url and text.startswith("ERROR: "):
        return "WebFetch", {"url": url.group(0), "prompt": "Summarize the page"}
    if "test session starts" in body or body.lstrip().startswith("collected"):
        return "Bash", {
            "command": "python -m unittest discover -s tests",
            "description": "Run the tests",
        }
    if body.startswith("--- a/"):
        return "Bash", {"command": "git diff", "description": "Show the changes"}
    if re.match(r"\S+\.py:\d+:", body):
        return "Bash", {"command": "grep -rn TODO .", "description": "Search for notes"}
    return "Bash", {"command": "make check", "description": "Run the checks"}


def tool_response(name: str, text: str) -> dict:
    return {"stdout": text, "stderr": "", "interrupted": False, "isImage": False}


def _row(kind: str, sid: str, content, **extra) -> dict:
    role = "assistant" if kind == "assistant" else "user"
    message = {"role": role, "content": content}
    if kind == "assistant":
        message["model"] = MODEL
    return {"type": kind, "sessionId": sid, "message": message, **extra}


def events(session: Session, cwd: str, transcript_path: str) -> Iterator[HookEvent]:
    """The hook events of the session, in the order the host fires them. Each carries the
    transcript lines written up to that moment; the caller writes them before running the hook."""
    sid = session.id
    base = {
        "session_id": sid,
        "transcript_path": transcript_path,
        "cwd": cwd,
        "permission_mode": "acceptEdits",
    }
    rows: list[dict] = []
    turns: tuple[Turn, ...] = session.turns
    calls = 0
    i = 0
    while i < len(turns):
        turn = turns[i]
        if turn.role != "user":  # text or a tool result with no prompt before it: skip it
            i += 1
            continue
        rows.append(_row("user", sid, turn.text))
        yield HookEvent(
            "UserPromptSubmit",
            {**base, "hook_event_name": "UserPromptSubmit", "prompt": turn.text},
            list(rows),
        )
        j = i + 1
        while j < len(turns) and turns[j].role != "user":
            j += 1
        last_text = ""
        for turn in turns[i + 1 : j]:
            if turn.role == "assistant":
                rows.append(_row("assistant", sid, [{"type": "text", "text": turn.text}]))
                last_text = turn.text
            else:
                calls += 1
                name, tool_input = tool_call(turn.text)
                use_id = f"toolu_{sid}_{calls:03d}"
                rows.append(
                    _row(
                        "assistant",
                        sid,
                        [{"type": "tool_use", "id": use_id, "name": name, "input": tool_input}],
                    )
                )
                failed = turn.text.startswith("ERROR: ")
                result = {"type": "tool_result", "tool_use_id": use_id, "content": turn.text}
                if failed:
                    result["is_error"] = True
                rows.append(_row("user", sid, [result]))
                if failed:
                    payload = {
                        **base,
                        "hook_event_name": "PostToolUseFailure",
                        "tool_name": name,
                        "tool_input": tool_input,
                        "tool_use_id": use_id,
                        "error": turn.text[len("ERROR: ") :],
                    }
                    yield HookEvent("PostToolUseFailure", payload, list(rows))
                else:
                    payload = {
                        **base,
                        "hook_event_name": "PostToolUse",
                        "tool_name": name,
                        "tool_input": tool_input,
                        "tool_response": tool_response(name, turn.text),
                        "tool_use_id": use_id,
                    }
                    yield HookEvent("PostToolUse", payload, list(rows))
                last_text = ""  # text before the next tool call is not the turn's last message
        stop = {**base, "hook_event_name": "Stop", "stop_hook_active": False}
        if last_text:
            stop["last_assistant_message"] = last_text
        yield HookEvent("Stop", stop, list(rows))
        i = j
    yield HookEvent(
        "SessionEnd", {**base, "hook_event_name": "SessionEnd", "reason": "other"}, list(rows)
    )


def write_transcript(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
