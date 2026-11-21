"""Passages: deterministic pieces of one observation, for ranking and for quoting.

An observation is the unit of evidence, authorization and purge; a passage is only a
view of it. Passages are computed from the text, never stored as evidence, and carry
exact offsets into the original, so the lexical index, the excerpt in a briefing and an
embedding of the first passage can all use the same boundaries. Rule `passages-v1`:

* Text is cut into units: a blank-line-delimited paragraph is read line by line; a line
  that looks like code or a path (indented four spaces or a tab, a fence, an operator,
  a call, a path with a slash or a file extension) is one unit and is never cut; any
  other line is cut after sentence ends (`.`, `!`, `?` followed by whitespace and not
  by a lowercase letter, or a CJK full stop).
* Units are packed into passages in order: a passage closes once it holds about 1,000
  characters, or at a paragraph end once it holds 600, and never grows past 1,200
  characters by adding a unit. No overlap. A last piece under 300 characters joins the
  passage before it, so a passage can reach 1,500 characters.
* A unit longer than 1,200 characters (2,000 for code or a path line) is cut at
  whitespace into pieces of at most 1,200 characters; a run with no whitespace at all is
  cut at 1,200 characters.
* An observation of at most 1,200 characters is one passage. Passages are contiguous and
  cover the text exactly, so every boundary is a unit boundary or a whitespace boundary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PASSAGE_RULE = "passages-v1"
MINIMUM = 600
TARGET = 1000
MAXIMUM = 1200
ATOMIC_MAXIMUM = 2000
TAIL = 300
EXCERPT_BYTES = 4096

_SENTENCE = re.compile(r"[.!?][\"')\]]*\s+(?=[^\sa-z])|[。！？][\"')\]]*")
_START = re.compile(
    r"\s*(?:```|~~~|\$ |>>> |#include\b|import |from \S+ import |def |class |function |const "
    r"|let |var |return\b|if \(|for \(|while \()"
)
_CALL = re.compile(r"\w\([^)]*\)\s*[:;{]?\s*$")
_OPERATORS = ("=>", "->", "::", "==", "!=", "&&", "||")
_SLASH = re.compile(r"(?:^|[\s(\"'`=])(?:~|\.{1,2})?/[\w.@~+-]+|[\w.-]+/[\w.-]+")
_EXTENSION = re.compile(r"\.(?:py|ts|tsx|js|json|md|sql|ya?ml|toml|go|rs|java|rb|sh|c|h|cpp|cs)\b")
_WINDOWS = re.compile(r"\b[A-Za-z]:\\\S")


@dataclass(frozen=True)
class Passage:
    start: int  # character offsets into the text
    end: int
    start_byte: int  # the same boundaries as UTF-8 byte offsets
    end_byte: int


def _atomic(line: str) -> bool:
    """Does this line look like code or a path? Cheap tests first: most lines are prose."""
    if line[0] == "\t" or line.startswith("    ") or _START.match(line):
        return True
    tail = line.rstrip()
    if tail and tail[-1] in "{};":
        return True
    if any(operator in line for operator in _OPERATORS):
        return True
    if "(" in line and _CALL.search(line):
        return True
    if "/" in line and _SLASH.search(line):
        return True
    if "\\" in line and _WINDOWS.search(line):
        return True
    return any(
        found.start() > 0 and (line[found.start() - 1].isalnum() or line[found.start() - 1] in "_-")
        for found in _EXTENSION.finditer(line)
    )


def _slice(text: str, start: int, end: int, limit: int) -> list[tuple[int, int]]:
    """Pieces of at most `limit` characters, cut after whitespace when there is any."""
    pieces = []
    while end - start > limit:
        cut = start + limit
        found = max(
            (i for i in range(cut, start + limit // 2, -1) if text[i - 1].isspace()),
            default=0,
        )
        cut = found or cut
        pieces.append((start, cut))
        start = cut
    pieces.append((start, end))
    return pieces


def _units(text: str) -> list[tuple[int, int, bool]]:
    """(start, end, ends a paragraph) spans that cover the text exactly."""
    units: list[tuple[int, int, bool]] = []
    cursor = position = 0
    for line in text.splitlines(keepends=True):
        end = position + len(line)
        if not line.strip():
            if units:
                units[-1] = (units[-1][0], end, True)
                cursor = end
            position = end
            continue
        if _atomic(line):
            cuts = [(cursor, end)]
            limit = ATOMIC_MAXIMUM
        else:
            marks = [position + m.end() for m in _SENTENCE.finditer(line)]
            bounds = [cursor, *[m for m in marks if cursor < m < end], end]
            cuts = [(a, b) for a, b in zip(bounds, bounds[1:], strict=False) if b > a]
            limit = MAXIMUM
        for a, b in cuts:
            pieces = _slice(text, a, b, MAXIMUM) if b - a > limit else [(a, b)]
            units.extend((x, y, False) for x, y in pieces)
        cursor = end
        position = end
    if units and units[-1][1] < len(text):
        units[-1] = (units[-1][0], len(text), units[-1][2])
    return units


def split(text: str) -> list[Passage]:
    """The passages of a text, in order, contiguous, covering it exactly."""
    if not text:
        return []
    if len(text) <= MAXIMUM:
        spans = [(0, len(text))]
    else:
        spans = []
        start: int | None = None
        end = 0
        previous_paragraph = False
        for a, b, paragraph in _units(text):
            if start is not None:
                size = end - start
                if (
                    size >= TARGET
                    or (size >= MINIMUM and previous_paragraph)
                    or size + (b - a) > MAXIMUM
                ):
                    spans.append((start, end))
                    start = None
            if start is None:
                start = a
            end = b
            previous_paragraph = paragraph
        if start is not None:
            if spans and end - start < TAIL:
                spans[-1] = (spans[-1][0], end)
            else:
                spans.append((start, end))
    passages: list[Passage] = []
    byte = 0
    last = 0
    for a, b in spans:
        byte += len(text[last:a].encode()) if a > last else 0
        size = len(text[a:b].encode())
        passages.append(Passage(a, b, byte, byte + size))
        byte += size
        last = b
    return passages


def excerpt(
    text: str, passages: list[Passage], scores: list[int], limit: int = EXCERPT_BYTES
) -> tuple[int, int]:
    """The character span to quote: the whole text when it fits, else the best passage
    and the contiguous passages next to it that score at least three quarters as well, while they fit
    in `limit` bytes.

    One quote is one byte range, because `fetch` returns exactly a range; a second-best
    passage that is not adjacent is therefore not joined. Without any match (an anchor or
    semantic hit) the first passage is quoted. The span is trimmed of surrounding
    whitespace and, if one passage is longer than the limit, cut at a word boundary."""
    if not passages:
        return 0, 0
    if passages[-1].end_byte <= limit:
        return 0, len(text)
    best = max(range(len(passages)), key=lambda i: (scores[i], -i))
    low = high = best
    used = passages[best].end_byte - passages[best].start_byte
    while True:
        options = []
        if low > 0 and scores[low - 1] * 4 >= scores[best] * 3 > 0:
            options.append((scores[low - 1], -1))
        if high + 1 < len(passages) and scores[high + 1] * 4 >= scores[best] * 3 > 0:
            options.append((scores[high + 1], 1))
        # the better neighbour first; on a tie, the one that follows
        options.sort(key=lambda o: (-o[0], -o[1]))
        for _, side in options:
            index = low - 1 if side < 0 else high + 1
            size = passages[index].end_byte - passages[index].start_byte
            if used + size <= limit:
                used += size
                low, high = (index, high) if side < 0 else (low, index)
                break
        else:
            break
    start, end = passages[low].start, passages[high].end
    while len(text[start:end].encode()) > limit:
        cut = max((i for i in range(end - 1, start, -1) if text[i - 1].isspace()), default=0)
        end = cut if cut > start else end - 1
    chunk = text[start:end]
    start += len(chunk) - len(chunk.lstrip())
    end -= len(chunk) - len(chunk.rstrip())
    return start, max(start, end)
