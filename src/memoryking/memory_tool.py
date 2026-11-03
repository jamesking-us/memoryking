"""Memory as files: Anthropic's memory tool and a Markdown vault over MemoryKing records.

The memory tool is a file interface with six commands (`view`, `create`, `str_replace`,
`insert`, `delete`, `rename`) under a `/memories` root. This adapter answers them from a
MemoryKing project, so a Claude API agent uses MemoryKing as its memory backend with no
prompt changes and every write is an ordinary receipted capture or proposal:

    /memories/<project>/README.md             what this memory is, generated
    /memories/<project>/<source>/<name>.md    one observation per file, newest version
    /memories/<project>/claims/<key>.md       one reviewed claim per family

A directory is a source; a file is the newest version of a note whose event id is its
name; its earlier versions remain evidence and are counted in the provenance block
that opens every file. Editing a file captures a new version, creating a file under
`claims/` proposes a claim that waits for review, viewing a file is a receipted fetch,
and deleting or renaming needs an administrator because memory is evidence. The same
file model is what `memoryking vault export` writes for hand editing and portability.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from memoryking.domain.hints import hint
from memoryking.domain.local import LocalError
from memoryking.sdk import LocalClient

ROOT = "/memories"
README = "README.md"
CLAIMS = "claims"
SUFFIX = ".md"
NAME_LIMIT = 80  # leaves room for a `__v<n>` version suffix inside a 96-char identifier
TEXT_LIMIT = 65536  # the capture contract's text bound, in bytes
CLAIM_TEXT_LIMIT = 8192
FETCH_CHUNK = 16384  # the fetch contract's largest range
LISTING_LIMIT = 256  # directory entries shown before the listing says what it left out
PAGE = 512
DAY_MS = 86_400_000
VERSION = re.compile(r"^(.+?)__v([0-9]+)$")
IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,96}$")
DEFINITION = {"type": "memory_20250818", "name": "memory"}


class MemoryToolError(Exception):
    """A message for the model, returned as a tool result with `is_error`."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class Location:
    kind: str  # root | project | readme | directory | file
    source: str | None = None
    name: str | None = None

    @property
    def claim(self) -> bool:
        return self.source == CLAIMS

    def path(self, project: str) -> str:
        if self.kind == "root":
            return ROOT
        if self.kind == "project":
            return f"{ROOT}/{project}"
        if self.kind == "readme":
            return f"{ROOT}/{project}/{README}"
        if self.kind == "directory":
            return f"{ROOT}/{project}/{self.source}"
        return f"{ROOT}/{project}/{self.source}/{self.name}{SUFFIX}"


def sanitize(part: str) -> str:
    """A file or directory name as the identifier it maps to; deterministic, bounded."""
    if part.endswith(SUFFIX):
        part = part[: -len(SUFFIX)]
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", part).strip("-")[:NAME_LIMIT]
    if not name:
        raise MemoryToolError("Error: names need at least one letter, digit, `_` or `-`.")
    if VERSION.match(name):
        raise MemoryToolError("Error: the `__v<number>` suffix is reserved for versions.")
    return name


def locate(path: Any, project: str) -> Location:
    """Resolve a memory path, refusing anything outside the root or shaped like traversal."""
    if not isinstance(path, str) or not (path == ROOT or path.startswith(ROOT + "/")):
        raise MemoryToolError(f"Error: The path {path} is outside {ROOT}.")
    parts = [p for p in path[len(ROOT) :].split("/") if p]
    for part in parts:
        if part in {".", ".."} or part.startswith(".") or "\\" in part or "%" in part:
            raise MemoryToolError(f"Error: The path {path} is not a valid memory path.")
    if not parts:
        return Location("root")
    if parts[0] != project:
        raise MemoryToolError(f"The path {path} does not exist. Please provide a valid path.")
    if len(parts) == 1:
        return Location("project")
    if len(parts) == 2:
        if parts[1] == README:
            return Location("readme")
        return Location("directory", sanitize(parts[1]))
    if len(parts) == 3:
        return Location("file", sanitize(parts[1]), sanitize(parts[2]))
    raise MemoryToolError(f"The path {path} does not exist. Please provide a valid path.")


def stamp(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def human(size: int) -> str:
    if size >= 1048576:
        return f"{size / 1048576:.1f}M"
    return f"{max(size, 1) / 1024:.1f}K"


def render(entry: dict[str, Any], project: str) -> str:
    """A file: a provenance block, then the text exactly as stored."""
    if entry["kind"] == "claim":
        head = [
            "memoryking: claim",
            f"revision: {entry['revision']}",
            f"family: {entry['family']}",
            f"key: {entry['logical_key']}",
            f"family_version: {entry['family_version']}",
            f"state: {entry['state']}" + (" (quarantined)" if entry["quarantined"] else ""),
            f"project: {project}",
            f"issuer: {entry['issuer']}",
            "evidence: [" + ", ".join(entry["evidence"]) + "]",
            f"valid_start: {entry['valid_start']}",
            f"valid_end: {entry['valid_end']}",
            f"sha256: {entry['hash']}",
        ]
    else:
        newest = entry["versions"][0]
        head = [
            "memoryking: observation",
            f"id: {newest['id']}",
            f"project: {project}",
            f"source: {entry['source']}",
            f"name: {entry['name']}",
            f"version: {len(entry['versions'])}",
            f"captured_by: {newest['principal']}",
            f"observed: {stamp(newest['observed_at_ms'])}",
            f"recorded: {stamp(newest['recorded_ms'])}",
            f"classification: {newest['classification']}",
            f"sha256: {newest['hash']}",
        ]
    body: str = entry["text"]
    return "---\n" + "\n".join(head) + "\n---\n" + body


def split(content: str) -> tuple[dict[str, Any], str]:
    """(provenance fields, body) of a file; a file without a leading block is all body."""
    if not content.startswith("---\n"):
        return {}, content
    end = content.find("\n---", 4)
    if end < 0:
        return {}, content
    fields: dict[str, Any] = {}
    for line in content[4:end].split("\n"):
        key, _, value = line.partition(":")
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            fields[key.strip()] = [v.strip() for v in value[1:-1].split(",") if v.strip()]
        else:
            fields[key.strip()] = value
    rest = content[end + 4 :]
    if rest.startswith("\n"):
        rest = rest[1:]
    return fields, rest


def numbered(content: str, path: str, view_range: Any = None) -> str:
    lines = content[:-1].split("\n") if content.endswith("\n") else content.split("\n")
    if len(lines) > 999_999:
        raise MemoryToolError(f"File {path} exceeds maximum line limit of 999,999 lines.")
    start, end = 1, len(lines)
    if view_range is not None:
        if (
            not isinstance(view_range, list)
            or len(view_range) != 2
            or not all(isinstance(v, int) and not isinstance(v, bool) for v in view_range)
            or not 1 <= view_range[0] <= len(lines)
            or (view_range[1] != -1 and view_range[1] < view_range[0])
        ):
            raise MemoryToolError(
                f"Error: Invalid `view_range` parameter: {view_range}. It should be [start, end] "
                f"within the {len(lines)} lines of the file, or [start, -1]."
            )
        start = view_range[0]
        end = len(lines) if view_range[1] == -1 else min(view_range[1], len(lines))
    shown = "\n".join(f"{i:6d}\t{lines[i - 1]}" for i in range(start, end + 1))
    return f"Here's the content of {path} with line numbers:\n{shown}"


def field(command: Any, key: str, default: Any = None) -> Any:
    """A command field, from a dict (the wire shape) or an SDK command object."""
    if isinstance(command, dict):
        return command.get(key, default)
    return getattr(command, key, default)


def same(a: str, b: str) -> bool:
    return a.rstrip("\n") == b.rstrip("\n")


class Files:
    """The project's records as files, over a bound client: list, read, write, propose."""

    def __init__(self, client: LocalClient):
        self.client = client
        self.project = client.project
        self.principal = client.binding[2]

    def _catalog(self, kind: str, **filters: Any) -> dict[str, Any]:
        return self.client.inspection(
            {
                "version": 1,
                "incarnation": self.client.binding[-1],
                "action": "catalog",
                "args": {"project": self.project, "kind": kind, **filters},
            }
        )

    def pages(self, kind: str, *, text: bool, **filters: Any) -> list[dict[str, Any]]:
        """Every item of a catalog, page after page."""
        items: list[dict[str, Any]] = []
        after: int | None = None
        while True:
            page = self._catalog(
                kind,
                text=text,
                limit=PAGE,
                **({"after": after} if after is not None else {}),
                **filters,
            )
            items.extend(page["items"])
            after = page["next_after"]
            if after is None or not page["items"]:
                return items

    @staticmethod
    def group(items: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
        """Observations as files keyed (source, name); versions newest first."""
        files: dict[tuple[str, str], dict[str, Any]] = {}
        for item in items:  # newest first already
            match = VERSION.match(item["source_event"])
            name = match.group(1) if match else item["source_event"]
            entry = files.setdefault(
                (item["source"], name),
                {"kind": "observation", "source": item["source"], "name": name, "versions": []},
            )
            entry["versions"].append(item)
        for entry in files.values():
            entry["text"] = entry["versions"][0].get("text", "")
        return files

    def listing(self, source: str | None = None, *, text: bool = False) -> dict[str, Any]:
        """One page of files (the newest `PAGE` observations) plus the total."""
        filters = {"source": source} if source else {}
        page = self._catalog("observations", text=text, limit=PAGE, **filters)
        return {"files": self.group(page["items"]), "total": page["total"]}

    def observation(self, source: str, name: str, *, text: bool = True) -> dict[str, Any] | None:
        page = self._catalog("observations", source=source, name=name, text=text, limit=PAGE)
        files = self.group(page["items"])
        return files.get((source, name))

    def by_id(self, observation: str) -> dict[str, Any] | None:
        page = self._catalog("observations", id=observation, text=False, limit=1)
        return page["items"][0] if page["items"] else None

    def claims(self, *, text: bool = False) -> dict[str, dict[str, Any]]:
        page = self._catalog("claims", text=text, limit=PAGE)
        return {item["logical_key"]: {**item, "kind": "claim"} for item in page["items"]}

    def claim(self, key: str) -> dict[str, Any] | None:
        page = self._catalog("claims", name=key, text=True, limit=1)
        return {**page["items"][0], "kind": "claim"} if page["items"] else None

    def fetch(self, kind: str, reference: str, size: int) -> str:
        """The record's text through receipted fetches, so a view counts as a delivery."""
        pieces = []
        for start in range(0, size, FETCH_CHUNK):
            packet = self.client.tool(
                "memory_fetch",
                {
                    "action": "fetch",
                    "args": {
                        "kind": kind,
                        "reference": reference,
                        "start_byte": start,
                        "end_byte": min(start + FETCH_CHUNK, size),
                        "max_output_tokens": 65536,
                    },
                },
            )
            if packet["status"] != "partial":
                raise LocalError(packet.get("reason", "unavailable"))
            pieces.append(packet["excerpt"])
        return "".join(pieces)

    def capture(
        self, source: str, name: str, text: str, versions: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """A new version of a file: a capture whose event id carries the version."""
        if not IDENTIFIER.match(source) or source == CLAIMS:
            raise MemoryToolError(f"Error: {source} is not a directory notes can be written to.")
        if not text.strip():
            raise MemoryToolError("Error: a memory file needs some text.")
        if len(text.encode()) > TEXT_LIMIT:
            raise MemoryToolError(
                f"Error: files are limited to {TEXT_LIMIT} bytes; split this one."
            )
        mine = {v["source_event"] for v in versions if v["principal"] == self.principal}
        number = len(versions) + 1
        event = name if not versions else f"{name}__v{number}"
        while event in mine:
            number += 1
            event = f"{name}__v{number}"
        key = "mt-" + hashlib.sha256(f"{source}\n{event}\n{text}".encode()).hexdigest()[:40]
        observed = int(time.time() * 1000) // DAY_MS * DAY_MS
        result = self.client.capture(
            key,
            {
                "version": 1,
                "project": self.project,
                "source": source,
                "source_event": event,
                "text": text,
                "readers": None,
                "classification": "internal",
                "observed_at_ms": observed,
            },
        )
        return {**result, "version": len(versions) + 1, "source_event": event}

    def spans(self, evidence: list[str]) -> list[dict[str, Any]]:
        """Whole-record evidence spans for observation ids or memory paths (`source/name`)."""
        spans = []
        for item in evidence:
            found = None
            if item.startswith("obs_"):
                found = self.by_id(item)
            else:
                parts = [
                    p for p in item.replace(ROOT + "/" + self.project + "/", "").split("/") if p
                ]
                if len(parts) == 2:
                    entry = self.observation(sanitize(parts[0]), sanitize(parts[1]), text=False)
                    found = entry["versions"][0] if entry else None
            if found is None:
                raise MemoryToolError(
                    f"Error: evidence `{item}` is not an observation id or a memory file "
                    "(`<source>/<name>`) in this project."
                )
            spans.append(
                {
                    "observation": found["id"],
                    "start_byte": 0,
                    "end_byte": found["bytes"],
                    "content_hash": found["hash"],
                }
            )
        if not spans:
            raise MemoryToolError(
                "Error: a claim needs evidence. Start the file with a block such as\n"
                "---\nevidence: [notes/decision, pytest/ev-123]\n---\n"
                "naming the memory files or observation ids that support it."
            )
        return spans[:16]

    def propose(
        self, key: str, text: str, evidence: list[str], existing: dict[str, Any] | None
    ) -> dict[str, Any]:
        """A claim proposal from a file: it waits for review like any other."""
        if not text.strip():
            raise MemoryToolError("Error: a claim needs some text.")
        if len(text.encode()) > CLAIM_TEXT_LIMIT:
            raise MemoryToolError(f"Error: claims are limited to {CLAIM_TEXT_LIMIT} bytes.")
        if not IDENTIFIER.match(key):
            raise MemoryToolError(
                "Error: a claim's name is an identifier of at most 96 characters."
            )
        version = existing["family_version"] if existing else 0
        body = {
            "text": text,
            "kind": "knowledge",
            "selector": {"selector_version": "1", "any_of": [{"all_of": [{"op": "any"}]}]},
            "valid_start": None,
            "valid_end": None,
            "task": None,
            "run": None,
            "readers": None,
            "evidence": self.spans(evidence),
            "authority": None,
            "rule_key": None,
            "effect": None,
            "rule_value": None,
            "provenance": {
                "method": "memory_file_v1",
                "path": f"{CLAIMS}/{key}{SUFFIX}",
                "proposed_by": self.principal,
            },
        }
        return self.client.knowledge(
            {
                "version": 1,
                "incarnation": self.client.binding[-1],
                "action": "propose",
                "key": "mt-"
                + hashlib.sha256(f"{key}\n{version}\n{text}".encode()).hexdigest()[:40],
                "args": {
                    "project": self.project,
                    "logical_key": key,
                    "expected_version": version,
                    "body": body,
                },
            }
        )

    def readme(self, files: dict[tuple[str, str], dict[str, Any]], total: int, claims: int) -> str:
        sources = sorted({source for source, _ in files})
        return (
            f"# Memory for project {self.project}\n\n"
            "This is a MemoryKing project seen as files. Every directory is a source and every\n"
            "file is one observation: the newest version of a note whose name is its event id.\n"
            "Each file opens with a provenance block (who captured it, when, which version); the\n"
            "text below the block is what was recorded. Editing or re-creating a file adds a\n"
            "version and keeps the earlier ones as evidence; nothing is overwritten.\n\n"
            f"`{CLAIMS}/` holds reviewed knowledge, one file per claim. A file created or edited\n"
            "there is a proposal that waits for review; it needs an `evidence: [...]` line in a\n"
            "leading `---` block naming the files or observation ids that support it. Deleting or\n"
            "renaming needs an administrator credential, because memory is evidence.\n\n"
            f"Sources: {', '.join(sources) if sources else 'none yet'}. Observations: {total}. "
            f"Claims: {claims}.\n"
        )


class MemoryTool:
    """The six memory-tool commands over a MemoryKing project.

    `execute` takes the command as the API sends it (a dict, or an SDK command object
    with the same attribute names) and returns the text for the tool result; it raises
    `MemoryToolError` with the text to return with `is_error`. `tool_result` builds the
    `tool_result` block directly. The definition to send in `tools` is `DEFINITION`.
    """

    definition = DEFINITION

    def __init__(self, client: LocalClient):
        self.files = Files(client)
        self.project = client.project

    def execute(self, command: Any) -> str:
        name = field(command, "command")
        handler = {
            "view": self.view,
            "create": self.create,
            "str_replace": self.str_replace,
            "insert": self.insert,
            "delete": self.delete,
            "rename": self.rename,
        }.get(name)
        if handler is None:
            raise MemoryToolError(f"Error: unknown command {name}")
        try:
            return handler(command)
        except LocalError as exc:
            raise MemoryToolError(f"Error: {exc.code}: {exc.hint or hint(exc.code)}") from exc

    def tool_result(self, tool_use_id: str, command: Any) -> dict[str, Any]:
        try:
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": self.execute(command),
            }
        except MemoryToolError as exc:
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": exc.message,
                "is_error": True,
            }

    # Reading

    def _entry(self, location: Location, *, text: bool) -> dict[str, Any]:
        if location.claim:
            entry = self.files.claim(location.name or "")
        else:
            entry = self.files.observation(location.source or "", location.name or "", text=text)
        if entry is None:
            raise MemoryToolError(
                f"The path {location.path(self.project)} does not exist. Please provide a valid path."
            )
        return entry

    def _content(self, location: Location) -> str:
        """The rendered file, its text read through receipted fetches."""
        entry = self._entry(location, text=False)
        if entry["kind"] == "claim":
            entry["text"] = self.files.fetch("revision", entry["revision"], entry["bytes"])
        else:
            newest = entry["versions"][0]
            entry["text"] = self.files.fetch("observation", newest["id"], newest["bytes"])
        return render(entry, self.project)

    def view(self, command: Any) -> str:
        location = locate(field(command, "path"), self.project)
        path = location.path(self.project)
        if location.kind == "readme":
            listing = self.files.listing()
            return numbered(
                self.files.readme(listing["files"], listing["total"], len(self.files.claims())),
                path,
                field(command, "view_range"),
            )
        if location.kind == "file":
            return numbered(self._content(location), path, field(command, "view_range"))
        return self._directory(location)

    def _directory(self, location: Location) -> str:
        project = f"{ROOT}/{self.project}"
        lines: list[tuple[int, str]] = []
        left = 0
        if location.kind == "root":
            lines.append((0, ROOT))
            lines.append((0, project))
            listing = self.files.listing()
            lines.append(
                (
                    len(self.files.readme(listing["files"], listing["total"], 0)),
                    f"{project}/{README}",
                )
            )
            lines.append((0, f"{project}/{CLAIMS}"))
            for source in sorted({s for s, _ in listing["files"]}):
                lines.append((0, f"{project}/{source}"))
        elif location.kind == "project":
            lines.append((0, project))
            listing = self.files.listing()
            claims = self.files.claims()
            readme = self.files.readme(listing["files"], listing["total"], len(claims))
            lines.append((len(readme), f"{project}/{README}"))
            lines.append((sum(c["bytes"] for c in claims.values()), f"{project}/{CLAIMS}"))
            for key, claim in sorted(claims.items()):
                lines.append((claim["bytes"], f"{project}/{CLAIMS}/{key}{SUFFIX}"))
            by_source: dict[str, list[dict[str, Any]]] = {}
            for (source, _), entry in listing["files"].items():
                by_source.setdefault(source, []).append(entry)
            for source in sorted(by_source):
                entries = by_source[source]
                lines.append(
                    (sum(e["versions"][0]["bytes"] for e in entries), f"{project}/{source}")
                )
                for entry in entries:
                    lines.append(
                        (
                            entry["versions"][0]["bytes"],
                            f"{project}/{source}/{entry['name']}{SUFFIX}",
                        )
                    )
            left = listing["total"] - sum(len(e["versions"]) for e in listing["files"].values())
        elif location.claim:
            claims = self.files.claims()
            lines.append((sum(c["bytes"] for c in claims.values()), f"{project}/{CLAIMS}"))
            for key, claim in sorted(claims.items()):
                lines.append((claim["bytes"], f"{project}/{CLAIMS}/{key}{SUFFIX}"))
        else:
            listing = self.files.listing(location.source)
            if not listing["files"]:
                raise MemoryToolError(
                    f"The path {location.path(self.project)} does not exist. Please provide a valid path."
                )
            entries = list(listing["files"].values())
            lines.append(
                (sum(e["versions"][0]["bytes"] for e in entries), f"{project}/{location.source}")
            )
            for entry in entries:
                lines.append(
                    (
                        entry["versions"][0]["bytes"],
                        f"{project}/{location.source}/{entry['name']}{SUFFIX}",
                    )
                )
            left = listing["total"] - sum(len(e["versions"]) for e in entries)
        shown = lines[:LISTING_LIMIT]
        text = "\n".join(f"{human(size)}\t{path}" for size, path in shown)
        notes = []
        if len(lines) > len(shown):
            notes.append(
                f"{len(lines) - len(shown)} more entries not listed; view a directory to list it."
            )
        if left > 0:
            notes.append(f"{left} older observations not listed; the newest {PAGE} are shown.")
        header = (
            f"Here're the files and directories up to 2 levels deep in {location.path(self.project)}, "
            "excluding hidden items and node_modules:\n"
        )
        return header + text + ("".join("\n" + n for n in notes))

    # Writing

    def _writable(self, command: Any, key: str = "path") -> Location:
        location = locate(field(command, key), self.project)
        if location.kind != "file":
            raise MemoryToolError(
                f"Error: write to a file under {ROOT}/{self.project}/<directory>/<name>{SUFFIX}; "
                f"{README} is generated and directories are sources."
            )
        return location

    def _write(self, location: Location, text: str, entry: dict[str, Any] | None) -> str:
        """Store new content for a file; the note tells the model what was recorded."""
        if location.claim:
            fields, body = split(text)
            evidence = (
                fields.get("evidence", []) if isinstance(fields.get("evidence"), list) else []
            )
            if entry and not evidence:
                evidence = entry["evidence"]
            result = self.files.propose(location.name or "", body, evidence, entry)
            return (
                f" (proposed as revision {result['revision']} of claim {location.name}; "
                "it waits for review and is not knowledge until accepted)"
            )
        versions = entry["versions"] if entry else []
        result = self.files.capture(location.source or "", location.name or "", text, versions)
        note = f" (version {result['version']}, observation {result['observation']}"
        if result["version"] > 1:
            note += "; earlier versions are kept as evidence"
        if result.get("held"):
            note += f"; held for review because source {location.source} is untrusted"
        return note + ")"

    def create(self, command: Any) -> str:
        location = self._writable(command)
        text = field(command, "file_text")
        if not isinstance(text, str):
            raise MemoryToolError("Error: `file_text` is required for create.")
        if location.claim:
            entry = self.files.claim(location.name or "")
        else:
            entry = self.files.observation(location.source or "", location.name or "")
        path = location.path(self.project)
        if entry is not None and same(split(text)[1] if location.claim else text, entry["text"]):
            return f"File created successfully at: {path} (unchanged)"
        return f"File created successfully at: {path}" + self._write(location, text, entry)

    def _edit_target(self, command: Any) -> tuple[Location, dict[str, Any], str, int]:
        """(location, entry, body, provenance line count) of a file to edit."""
        location = locate(field(command, "path"), self.project)
        if location.kind != "file":
            raise MemoryToolError(
                f"Error: The path {location.path(self.project)} does not exist. Please provide a valid path."
            )
        entry = self._entry(location, text=True)
        rendered = render(entry, self.project)
        head = rendered[: len(rendered) - len(entry["text"])]
        return location, entry, entry["text"], head.count("\n")

    def str_replace(self, command: Any) -> str:
        location, entry, body, head = self._edit_target(command)
        path = location.path(self.project)
        old = field(command, "old_str")
        new = field(command, "new_str") or ""
        if not isinstance(old, str) or not old:
            raise MemoryToolError("Error: `old_str` is required for str_replace.")
        count = body.count(old)
        if count == 0:
            rendered = render(entry, self.project)
            if old in rendered[: len(rendered) - len(body)]:
                raise MemoryToolError(
                    f"Error: lines 1-{head} of {path} are provenance and cannot be edited; "
                    "edit the text below them."
                )
            raise MemoryToolError(
                f"No replacement was performed, old_str `{old}` did not appear verbatim in {path}."
            )
        if count > 1:
            numbers = [
                head + body[:index].count("\n") + 1
                for index in range(len(body))
                if body.startswith(old, index)
            ]
            raise MemoryToolError(
                f"No replacement was performed. Multiple occurrences of old_str `{old}` in lines: "
                f"{', '.join(str(n) for n in numbers)}. Please ensure it is unique"
            )
        updated = body.replace(old, new, 1)
        note = self._write(location, updated, entry)
        line = head + body[: body.index(old)].count("\n") + 1
        snippet = self._snippet(entry, updated, line, new.count("\n") + 1)
        return f"The memory file has been edited.{note}\n{snippet}"

    def _snippet(self, entry: dict[str, Any], text: str, line: int, span: int) -> str:
        preview = render({**entry, "text": text}, self.project)
        lines = preview[:-1].split("\n") if preview.endswith("\n") else preview.split("\n")
        start, end = max(1, line - 3), min(len(lines), line + span + 2)
        return "\n".join(f"{i:6d}\t{lines[i - 1]}" for i in range(start, end + 1))

    def insert(self, command: Any) -> str:
        location, entry, body, head = self._edit_target(command)
        path = location.path(self.project)
        at = field(command, "insert_line")
        text = field(command, "insert_text")
        if not isinstance(text, str):
            raise MemoryToolError("Error: `insert_text` is required for insert.")
        trailing = body.endswith("\n")
        lines = (body[:-1] if trailing else body).split("\n")
        total = head + len(lines)
        if not isinstance(at, int) or isinstance(at, bool) or not head <= at <= total:
            raise MemoryToolError(
                f"Error: Invalid `insert_line` parameter: {at}. It should be within the range of "
                f"lines of the file: [{head}, {total}] (lines 1-{head} are provenance)"
            )
        lines.insert(at - head, text[:-1] if text.endswith("\n") else text)
        note = self._write(location, "\n".join(lines) + ("\n" if trailing else ""), entry)
        return f"The file {path} has been edited.{note}"

    # Removing

    def delete(self, command: Any) -> str:
        location = locate(field(command, "path"), self.project)
        path = location.path(self.project)
        if location.kind in {"root", "project", "readme"}:
            raise MemoryToolError(f"Error: {path} cannot be deleted.")
        if location.kind == "directory":
            raise MemoryToolError(
                f"Error: {path} is a directory of sources; delete its files one at a time."
            )
        if location.claim:
            raise MemoryToolError(
                "Error: claims retire through review, not deletion; edit the file to propose a correction."
            )
        entry = self._entry(location, text=False)
        self._purge(entry)
        return f"Successfully deleted {path} ({len(entry['versions'])} version(s) purged)"

    def _administrator(self) -> None:
        if "admin" not in self.files.client.context()["capabilities"]:
            raise MemoryToolError(
                "Error: memory is evidence; deleting or renaming it needs an administrator "
                "credential. Replace the file's content instead; earlier versions stay on record."
            )

    def _purge(self, entry: dict[str, Any]) -> None:
        self._administrator()
        for version in entry["versions"]:
            self.files.client.purge(version["id"])

    def rename(self, command: Any) -> str:
        source = locate(field(command, "old_path"), self.project)
        target = locate(field(command, "new_path"), self.project)
        old, new = source.path(self.project), target.path(self.project)
        if source.kind != "file" or source.claim:
            raise MemoryToolError(f"Error: {old} cannot be renamed; only notes can.")
        if target.kind != "file" or target.claim:
            raise MemoryToolError(f"Error: {new} is not a note path.")
        entry = self._entry(source, text=True)
        if self.files.observation(target.source or "", target.name or "", text=False) is not None:
            raise MemoryToolError(f"Error: The destination {new} already exists")
        self._administrator()
        self.files.capture(target.source or "", target.name or "", entry["text"], [])
        self._purge(entry)
        return f"Successfully renamed {old} to {new}"
