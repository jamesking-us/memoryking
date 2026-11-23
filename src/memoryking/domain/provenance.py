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


# --- Third-party text a conversation turn quoted -----------------------------------------
#
# A turn of the assistant (or the user) may copy what a tool returned or a page said into its
# own words. The copy is still third-party text. A capture finds it by comparing the turn with
# the tool results of the same session, and only that: a run of QUOTE_WORDS or more consecutive
# words (letters and digits, no case, no punctuation, no spacing) of the turn that stands
# consecutively in one tool result, and is at least QUOTE_CHARS characters long. Nothing is read
# from how the text is worded.

QUOTE_WORDS = 12
QUOTE_CHARS = 72
QUOTED_PREFIX = "Quoted by "  # opens each paragraph of a third-party record made of copied text
_WORD = re.compile(r"[^\W_]+")


class QuoteIndex:
    """The runs of QUOTE_WORDS consecutive words of some third-party texts."""

    def __init__(self, texts: list[str]) -> None:
        self._runs: set[tuple[str, ...]] = set()
        for text in texts:
            words = [m.group().casefold() for m in _WORD.finditer(text)]
            for i in range(len(words) - QUOTE_WORDS + 1):
                self._runs.add(tuple(words[i : i + QUOTE_WORDS]))

    def __bool__(self) -> bool:
        return bool(self._runs)

    def spans(self, text: str) -> list[tuple[int, int]]:
        """The character ranges of `text` that are copies (see the rule above), in order."""
        if not self._runs:
            return []
        found = list(_WORD.finditer(text))
        low = [m.group().casefold() for m in found]
        covered = [False] * len(found)
        for i in range(len(found) - QUOTE_WORDS + 1):
            if tuple(low[i : i + QUOTE_WORDS]) in self._runs:
                for j in range(i, i + QUOTE_WORDS):
                    covered[j] = True
        spans: list[tuple[int, int]] = []
        i = 0
        while i < len(found):
            if not covered[i]:
                i += 1
                continue
            j = i
            while j + 1 < len(found) and covered[j + 1]:
                j += 1
            start, end = found[i].start(), found[j].end()
            if end - start >= QUOTE_CHARS:
                spans.append((start, end))
            i = j + 1
        return spans


def quote_marker(size: int) -> str:
    return f"[quoted text left out: {size} characters, kept in the tool-output record]"


def split_quotes(text: str, index: QuoteIndex) -> tuple[str, list[str]]:
    """(`text` with each copied span replaced by a marker, the spans in order)."""
    spans = index.spans(text)
    if not spans:
        return text, []
    out: list[str] = []
    taken: list[str] = []
    at = 0
    for start, end in spans:
        out.append(text[at:start])
        out.append(quote_marker(end - start))
        taken.append(text[start:end])
        at = end
    out.append(text[at:])
    return "".join(out), taken
