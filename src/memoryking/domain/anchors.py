"""Entity anchors: the exact names a developer's memory hangs on, found without a model.

An anchor is a file path, a code symbol, a ticket id, a URL, a hostname or an error
code found verbatim in an observation's text. Anchors are extracted deterministically at
capture time and stored beside the observation, so a briefing for "payments/retry.py"
finds every record that names that file even when the words around it differ, and the
graph can link two observations that share a rare anchor. Extraction is bounded and
exact: no inference, no normalisation beyond case for hosts and URLs, no model.
"""

from __future__ import annotations

import re

KINDS = ("path", "symbol", "ticket", "url", "host", "error")
MAX_ANCHORS = 32
MAX_LENGTH = 128
EXTENSIONS = (
    "py|ts|tsx|js|mjs|cjs|jsx|json|md|sql|yaml|yml|toml|ini|cfg|txt|go|rs|java|kt|rb|php|"
    "c|cc|cpp|h|hpp|cs|swift|m|sh|bash|zsh|ps1|html|css|scss|xml|proto|graphql|tf|lock|csv"
)
_PATH = re.compile(rf"(?<![\w/])((?:[\w.-]+/)+[\w.-]+|[\w-]+\.(?:{EXTENSIONS}))(?![\w/])")
_URL = re.compile(r"\bhttps?://[^\s<>\"')\]]+")
_HOST = re.compile(
    r"(?<![\w./@-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.){1,}"
    r"(?:[a-z]{2,24}))(?::\d{2,5})?(?![\w(-]|\.\w)",
    re.IGNORECASE,
)
_TICKET = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,7})\b|(?<![\w/])(#\d{2,7})\b")
_SYMBOL = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)(?=\()"  # a.b.c(
    r"|\b([A-Za-z_][A-Za-z0-9_]*)\(\)"  # name()
    r"|\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b"  # CamelCase with two humps
    r"|\b([a-z][a-z0-9]*(?:_[a-z0-9]+){1,})\b"  # snake_case with an underscore
)
_ERROR = re.compile(
    r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b"  # SCREAMING_SNAKE
    r"|\b(E[A-Z]{2,}|E\d{3,5}|0x[0-9A-Fa-f]{4,}|HTTP\s?[45]\d\d|[45]\d\d\s(?:Gateway|Not|Internal|Bad|Forbidden|Unauthorized))\b"
    r"|\b([A-Za-z]+(?:Error|Exception|Failure|Timeout))\b"
)
_COMMON_SNAKE = {"as_of", "e_g", "i_e"}


def extract(text: str) -> list[tuple[str, str]]:
    """(kind, anchor) pairs in first-seen order, at most MAX_ANCHORS, each bounded."""
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str) -> None:
        value = value.strip().rstrip(".,;:)")
        if not value or len(value) > MAX_LENGTH or (kind, value) in seen:
            return
        seen.add((kind, value))
        found.append((kind, value))

    for match in _URL.finditer(text):
        url = match.group(0).rstrip(".,;:)")
        add("url", url.lower())
        host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0].lower()
        if "." in host:
            add("host", host)
    stripped = _URL.sub(" ", text)
    for match in _PATH.finditer(stripped):
        value = match.group(1)
        if value.count("/") == 0 and not re.search(rf"\.(?:{EXTENSIONS})$", value):
            continue
        if re.fullmatch(r"[\d.]+", value):
            continue
        add("path", value)
    for match in _HOST.finditer(stripped):
        value = match.group(1).lower()
        if re.search(rf"\.(?:{EXTENSIONS})$", value) or re.fullmatch(r"[\d.]+", value):
            continue
        # A dotted code symbol is not a host: it is followed by a call or has no TLD-like end.
        if (
            _SYMBOL.match(stripped, match.start(1))
            and stripped[match.end(1) : match.end(1) + 1] == "("
        ):
            continue
        add("host", value)
    for match in _TICKET.finditer(stripped):
        add("ticket", match.group(1) or match.group(2))
    for match in _ERROR.finditer(stripped):
        add("error", match.group(1) or match.group(2) or match.group(3))
    for match in _SYMBOL.finditer(stripped):
        value = match.group(1) or match.group(2) or match.group(3) or match.group(4)
        if value in _COMMON_SNAKE or len(value) < 4:
            continue
        if ("error", value) in seen or re.fullmatch(r"[A-Z][a-z]+", value):
            continue
        add("symbol", value)
    return found[:MAX_ANCHORS]


def anchors_in(text: str) -> list[str]:
    """Just the anchor strings, for query-side matching."""
    return [value for _, value in extract(text)]
