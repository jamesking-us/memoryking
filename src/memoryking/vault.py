"""A Markdown vault: the project's memory as files on disk, for hand editing and portability.

`export` writes the same files the memory-tool adapter shows (`<project>/<source>/<name>.md`
for observations, `<project>/claims/<key>.md` for claims, each with a provenance block,
plus a README) into a private directory. `import_` reads such a directory back: a file
whose text differs from the stored newest version is captured as a new version, a new
file is a new observation, and a claim file is a proposal with the evidence its block
names. Nothing is deleted or overwritten in the store; the directory is the editable copy.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memoryking.adapters.local_files import private_dir
from memoryking.domain.local import LocalError
from memoryking.memory_tool import (
    CLAIMS,
    IDENTIFIER,
    README,
    SUFFIX,
    Files,
    MemoryToolError,
    render,
    same,
    split,
)
from memoryking.sdk import LocalClient


def connect(*, socket: Path | None, credential: Path | None, project: str | None) -> LocalClient:
    """A client from explicit paths, else the environment (`MEMORYKING_*`)."""
    socket = socket or (
        Path(os.environ["MEMORYKING_SOCKET"]) if os.environ.get("MEMORYKING_SOCKET") else None
    )
    credential = credential or (
        Path(os.environ["MEMORYKING_CREDENTIAL"])
        if os.environ.get("MEMORYKING_CREDENTIAL")
        else None
    )
    project = project or os.environ.get("MEMORYKING_PROJECT")
    if socket is None:
        raise LocalError("socket_required")
    if credential is None:
        raise LocalError("credential_required")
    if not project:
        raise LocalError("project_required")
    return LocalClient(socket, credential, project, namespace="vault")


def write(path: Path, data: bytes) -> None:
    """Replace a vault file with a private one, whatever a hand editor left its mode at."""
    temporary = path.with_name(".stage-" + secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def export(directory: Path, client: LocalClient) -> dict[str, Any]:
    """Write every visible observation (newest version per file) and claim as Markdown."""
    files = Files(client)
    root = directory / client.project
    private_dir(directory, create=True)
    private_dir(root, create=True)
    grouped = files.group(files.pages("observations", text=True))
    written: list[str] = []
    for (source, name), entry in sorted(grouped.items()):
        folder = root / source
        private_dir(folder, create=True)
        path = folder / f"{name}{SUFFIX}"
        write(path, render(entry, client.project).encode())
        written.append(f"{source}/{name}{SUFFIX}")
    claims = files.pages("claims", text=True)
    if claims:
        private_dir(root / CLAIMS, create=True)
    for item in claims:
        path = root / CLAIMS / f"{item['logical_key']}{SUFFIX}"
        write(path, render({**item, "kind": "claim"}, client.project).encode())
        written.append(f"{CLAIMS}/{item['logical_key']}{SUFFIX}")
    total = sum(len(entry["versions"]) for entry in grouped.values())
    readme = files.readme(grouped, total, len(claims)) + (
        f"\nExported {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} by "
        f"{files.principal}. Edit the text below a file's provenance block, add files, then\n"
        f"run `memoryking vault import --project {client.project} --dir <this directory's parent>`:\n"
        "changed and new files become new versions and observations, claim files become\n"
        "proposals. The provenance block is ignored on import except for `evidence`.\n"
    )
    write(root / README, readme.encode())
    return {
        "directory": str(root),
        "observations": len(grouped),
        "versions": total,
        "claims": len(claims),
        "files": written,
    }


def import_(directory: Path, client: LocalClient, *, dry_run: bool = False) -> dict[str, Any]:
    """Capture changed or new files as versions and claim files as proposals."""
    files = Files(client)
    root = directory / client.project
    if not root.is_dir():
        raise LocalError("vault_missing")
    captured: list[dict[str, Any]] = []
    proposed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    unchanged = 0
    folders = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    # Notes first, so a claim file can cite a note that the same import adds.
    for folder in sorted(folders, key=lambda p: p.name == CLAIMS):
        source = folder.name
        for path in sorted(p for p in folder.iterdir() if p.is_file() and p.suffix == SUFFIX):
            relative = f"{source}/{path.name}"
            name = path.name[: -len(SUFFIX)]
            try:
                if not IDENTIFIER.match(source) or not IDENTIFIER.match(name):
                    raise MemoryToolError(
                        "Error: directory and file names are identifiers (letters, digits, `_`, `-`)."
                    )
                fields, body = split(path.read_text(encoding="utf-8"))
                if source == CLAIMS:
                    existing = files.claim(name)
                    if existing is not None and same(existing["text"], body):
                        unchanged += 1
                        continue
                    evidence = (
                        fields.get("evidence") if isinstance(fields.get("evidence"), list) else []
                    )
                    if existing is not None and not evidence:
                        evidence = existing["evidence"]
                    record = {
                        "file": relative,
                        "key": name,
                        "version": (existing or {}).get("family_version", 0) + 1,
                    }
                    if not dry_run:
                        result = files.propose(name, body, evidence or [], existing)
                        record["revision"] = result["revision"]
                    else:
                        files.spans(evidence or [])  # the evidence must still resolve
                    proposed.append(record)
                else:
                    entry = files.observation(source, name)
                    if entry is not None and same(entry["text"], body):
                        unchanged += 1
                        continue
                    versions = entry["versions"] if entry else []
                    record = {"file": relative, "version": len(versions) + 1}
                    if not dry_run:
                        result = files.capture(source, name, body, versions)
                        record["observation"] = result["observation"]
                    captured.append(record)
            except (MemoryToolError, LocalError, UnicodeDecodeError) as exc:
                errors.append(
                    {
                        "file": relative,
                        "error": exc.message if isinstance(exc, MemoryToolError) else str(exc),
                    }
                )
    return {
        "directory": str(root),
        "dry_run": dry_run,
        "captured": captured,
        "proposed": proposed,
        "unchanged": unchanged,
        "errors": errors,
    }
