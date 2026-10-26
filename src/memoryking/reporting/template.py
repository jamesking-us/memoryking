"""Bounded phase document validation and renderer-neutral reader content."""

from __future__ import annotations

import re
from typing import Any

from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import LocalError
from memoryking.domain.reporting import CHUNK_LIMIT, SOURCE_LIMIT
from memoryking.reporting.document import blocks

TEMPLATE = "phase-report"
TEMPLATE_VERSION = 1
MAX_BLOCKS = 8192
MAX_COLUMNS = 16
FRAGMENT_CHARS = 4096


def model(source: str, title: str) -> dict[str, Any]:
    """Validate complete phase content; never infer factual truth or human approval."""
    try:
        source_size = len(source.encode("utf-8"))
    except UnicodeError as exc:
        raise LocalError("invalid_report_source") from exc
    if (
        not source
        or source_size > SOURCE_LIMIT
        or not title
        or len(title) > 512
        or source.splitlines()[0] != "# " + title
        or any(ord(c) < 32 and c not in "\r\n\t" for c in source)
    ):
        raise LocalError("invalid_report_source")
    document: list[dict[str, Any]] = []
    sections: list[int] = []
    try:
        for kind, value in blocks(source):
            if len(document) >= MAX_BLOCKS:
                raise LocalError("report_complexity_limit")
            item: dict[str, Any] = {"kind": kind}
            if kind == "heading":
                level, text = value
                if len(text) > 512 or (level == 1 and document):
                    raise LocalError("invalid_report_heading")
                item.update(level=level, text=text)
                if level == 2 and (match := re.match(r"^(\d+)\.\s+\S", text)):
                    sections.append(int(match[1]))
            elif kind == "table":
                if not value or not value[0] or len(value[0]) > MAX_COLUMNS:
                    raise LocalError("report_complexity_limit")
                item.update(headers=value[0], rows=value[1:])
            elif kind == "figure":
                caption, reference = value
                if len(caption) > 1024 or not re.fullmatch(
                    r"[A-Za-z0-9_-]{1,96}\.(?:png|jpg|jpeg)", reference
                ):
                    raise LocalError("invalid_report_figure")
                item.update(caption=caption, reference=reference)
            else:
                item["text"] = value
            document.append(item)
    except ValueError as exc:
        raise LocalError("invalid_report_source") from exc
    if sections != list(range(1, 18)):
        raise LocalError("incomplete_phase_report")
    return {
        "version": 1,
        "template": TEMPLATE,
        "template_version": TEMPLATE_VERSION,
        "title": title,
        "blocks": document,
    }


def reader_pages(document: dict[str, Any]) -> list[bytes]:
    """Lossless bounded block fragments, independent of a phone rendering framework.

    Content fragments are Unicode slices of canonical block JSON. Their ordered
    concatenation decodes to the exact block; the client must not execute markup.
    The service can authorize each page without loading the full document on a phone.
    A later reader may stream long text blocks; no content is silently truncated.
    """
    pages: list[bytes] = []
    for index, block in enumerate(document["blocks"]):
        encoded = canonical_bytes(block).decode("utf-8")
        fragments = [
            encoded[i : i + FRAGMENT_CHARS] for i in range(0, len(encoded), FRAGMENT_CHARS)
        ]
        for part, fragment in enumerate(fragments):
            page = canonical_bytes(
                {
                    "version": 1,
                    "block": index,
                    "part": part,
                    "parts": len(fragments),
                    "encoding": "canonical-block-json",
                    "content": fragment,
                }
            )
            if len(page) > CHUNK_LIMIT:
                raise LocalError("report_chunk_limit")
            pages.append(page)
    return pages
