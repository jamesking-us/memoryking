"""Controlled Markdown parser shared by reader, PDF and EPUB; no I/O or vendors."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any


def blocks(source: str) -> Iterator[tuple[str, Any]]:
    lines = source.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith("!["):
            match = re.fullmatch(r"!\[([^\]]+)\]\(([^)]+)\)", line)
            if not match:
                raise ValueError("Invalid figure syntax; caption and local path required")
            yield ("figure", (match[1], match[2]))
            i += 1
            continue
        if line.startswith("```"):
            code = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i])
                i += 1
            if i == len(lines):
                raise ValueError("Unterminated code fence")
            yield ("code", "\n".join(code))
            i += 1
            continue
        heading = re.match(r"^(#{1,6}) (.+)$", line)
        if heading:
            yield ("heading", (len(heading[1]), heading[2]))
            i += 1
            continue
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                row = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", c.replace(" ", "")) for c in row):
                    rows.append(row)
                i += 1
            if any(len(r) != len(rows[0]) for r in rows):
                raise ValueError("Unequal Markdown table columns")
            yield ("table", rows)
            continue
        item = re.match(r"^(?:[-*] |\d+\. )(.+)$", line)
        if item:
            yield ("item", line)
            i += 1
            continue
        para = [line]
        i += 1
        while (
            i < len(lines)
            and lines[i].strip()
            and not re.match(r"^(#|!\[|\||```|[-*] |\d+\. )", lines[i])
        ):
            para.append(lines[i])
            i += 1
        yield ("paragraph", " ".join(para))


def plain(value: str) -> str:
    return re.sub(r"[`*]", "", re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", value))
