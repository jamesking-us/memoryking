#!/usr/bin/env python3
"""Capture immutable Git snippets, complete change inventories, and checked manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args])


def revision(root: Path, value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("Use an immutable full commit SHA")
    resolved = git(root, "rev-parse", "--verify", value + "^{commit}").decode().strip()
    if resolved != value:
        raise ValueError("Revision must identify the exact commit")
    return resolved


def safe_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or ":" in value or "\n" in value or "\x00" in value:
        raise ValueError("Expected a repository-relative path")
    if any(p in {".git", ".phase-reports"} or p.startswith(".env") for p in path.parts):
        raise ValueError("Private delivery and environment files cannot be evidence")
    return str(path)


def snippet(root: Path, commit: str, path: str, start: int, end: int) -> dict:
    commit, path = revision(root, commit), safe_path(path)
    data = git(root, "show", f"{commit}:{path}")
    lines = data.splitlines(keepends=True)
    if not 1 <= start <= end <= len(lines):
        raise ValueError("Snippet bounds are outside the source")
    excerpt = b"".join(lines[start - 1 : end])
    return {
        "path": path,
        "commit": commit,
        "start_line": start,
        "end_line": end,
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "excerpt_sha256": hashlib.sha256(excerpt).hexdigest(),
        "text": excerpt.decode("utf-8"),
    }


def inventory(root: Path, baseline: str, commit: str) -> dict:
    baseline, commit = revision(root, baseline), revision(root, commit)
    entries = git(root, "diff", "--name-status", "-z", "--no-renames", baseline, commit).split(
        b"\0"
    )
    changes = []
    for index in range(0, len(entries) - 1, 2):
        status, path = entries[index].decode(), safe_path(entries[index + 1].decode())
        record = {"status": status, "path": path}
        if status != "D":
            content = git(root, "show", f"{commit}:{path}")
            record.update(sha256=hashlib.sha256(content).hexdigest(), bytes=len(content))
        changes.append(record)
    return {
        "baseline": baseline,
        "revision": commit,
        "rename_policy": "delete-and-add",
        "files": changes,
    }


def manifest(root: Path, paths: list[str]) -> dict:
    records = []
    for value in sorted(set(paths)):
        path = safe_path(value)
        target = (root / path).resolve()
        if not target.is_relative_to(root.resolve()) or not target.is_file():
            raise ValueError("Evidence must be a real file inside the repository")
        data = target.read_bytes()
        records.append(
            {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        )
    if not records:
        raise ValueError("Evidence manifest cannot be empty")
    return {"schema_version": 1, "files": records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    extract = commands.add_parser("snippet")
    extract.add_argument("--revision", required=True)
    extract.add_argument("--path", required=True)
    extract.add_argument("--start", type=int, required=True)
    extract.add_argument("--end", type=int, required=True)
    changes = commands.add_parser("inventory")
    changes.add_argument("--baseline", required=True)
    changes.add_argument("--revision", required=True)
    evidence = commands.add_parser("manifest")
    evidence.add_argument("paths", nargs="+")
    args = parser.parse_args()
    if args.command == "snippet":
        data = snippet(args.root, args.revision, args.path, args.start, args.end)
    elif args.command == "inventory":
        data = inventory(args.root, args.baseline, args.revision)
    else:
        data = manifest(args.root, args.paths)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Wrote {args.command}: {args.output}")


if __name__ == "__main__":
    main()
