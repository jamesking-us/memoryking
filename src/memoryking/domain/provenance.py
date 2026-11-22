"""Whose words a record holds: the user's and the assistant's, or a third party's.

A tool's result, a fetched page and a tool's error message are written by whatever the tool
reached, not by the user or the assistant, and may carry text meant to steer a model. A writer
says so with the record's `source` (a field every observation already has): the host hooks
write them as `<host>-tool`, and a script or agent that saves such text names a source that is
`tool`, `web` or `fetch`, alone or joined to a word by `-` or `_`. Nothing is read from the
text itself.

The automatic views (the session-start briefing and the prompt-time hook) leave third-party
records out unless the operator asks for them; an explicit `recall` returns them, fenced and
labelled (see `briefing_text`)."""

from __future__ import annotations

import re

KINDS = ("tool", "web", "fetch")
_KIND = "|".join(KINDS)
THIRD_PARTY_SOURCE = re.compile(rf"^(({_KIND})([-_].*)?|.*[-_]({_KIND}))$")

# The sources of what a host hook captured of a conversation at the end of a session or before
# it is compacted.
HOST_CAPTURE = re.compile(r"^(claude|codex|grok)-(session|compact)$")

# The line a host hook writes before a tool's error message in a conversation capture. Captures
# made before tool errors were stored apart hold them inside the session's own text.
TOOL_ERROR_PREFIX = "Tool error: "
_SPEAKER = re.compile(r"^(User|Assistant): |^" + re.escape(TOOL_ERROR_PREFIX))


def is_third_party(source: object) -> bool:
    """True when `source` says the record holds text a third party wrote."""
    return isinstance(source, str) and THIRD_PARTY_SOURCE.match(source) is not None


def without_tool_errors(text: str) -> str:
    """`text` with the tool errors a hook once wrote into a conversation capture left out.

    A capture is paragraphs, each opened by `User: `, `Assistant: ` or `Tool error: `; a
    paragraph that opens with none of them continues the one before. Only paragraphs of a
    tool error are dropped, so a record with none comes back unchanged."""
    if TOOL_ERROR_PREFIX not in text:
        return text
    kept: list[str] = []
    dropping = False
    for paragraph in re.split(r"\n\s*\n", text):
        opened = _SPEAKER.match(paragraph.lstrip())
        if opened:
            dropping = opened.group(0) == TOOL_ERROR_PREFIX
        if not dropping:
            kept.append(paragraph)
    return "\n\n".join(kept)
