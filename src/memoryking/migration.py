"""Operator-only snapshot planning/import; no source writes or live routing changes."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any

from memoryking.adapters.legacy_snapshot import MAX_DATABASE, digest_file, inspect
from memoryking.adapters.local_files import atomic, private_dir, private_file, sync_dir
from memoryking.application.legacy_import import apply_batch, make_plan, reconcile, summary
from memoryking.contracts.canonical import canonical_bytes, load_strict
from memoryking.domain.local import LocalError
from memoryking.sdk import LocalClient
from memoryking.shared_sdk import SharedClient


class Operator:
    def __init__(self, client: LocalClient):
        self.client = client

    def context(self) -> dict[str, Any]:
        return self.client.context()

    def capture(self, key: str, observation: dict[str, Any]) -> dict[str, Any]:
        return self.client.capture(key, observation)

    def read_legacy(self, handle: str) -> dict[str, Any]:
        credential, _ = self.client._current()
        return self.client._request(
            credential, "read", {"project": self.client.project, "observation": handle}
        )

    def knowledge(self, command: dict[str, Any]) -> dict[str, Any]:
        credential, _ = self.client._current()
        return self.client._request(credential, "knowledge", {"command": command})


def freeze(source: Path, directory: Path) -> Path:
    if (
        directory.exists()
        or directory.is_symlink()
        or not source.is_file()
        or source.is_symlink()
        or not 0 < source.stat().st_size <= MAX_DATABASE
    ):
        raise LocalError("legacy_snapshot_destination_or_source_invalid")
    if any(Path(str(source) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise LocalError("legacy_snapshot_not_detached")
    before = digest_file(source)
    private_dir(directory, create=True)
    target = directory / "source.sqlite"
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        os.chmod(target, 0o600)
        shutil.copyfileobj(incoming, outgoing)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    sync_dir(directory)
    if digest_file(target) != before or digest_file(source) != before:
        raise LocalError("legacy_source_changed")
    atomic(
        directory / "snapshot.json",
        canonical_bytes(
            {
                "sha256": before,
                "bytes": target.stat().st_size,
                "method": "detached_snapshot_copy_hash_checked",
                "source_modified": False,
            }
        ),
    )
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    copy = sub.add_parser(
        "snapshot", help="Copy a detached SQLite backup; rejects WAL/journal sidecars"
    )
    copy.add_argument("--source", type=Path, required=True)
    copy.add_argument("--directory", type=Path, required=True)
    for name in ("inspect", "plan", "apply", "reconcile"):
        command = sub.add_parser(name)
        command.add_argument("--directory", type=Path, required=True)
        if name in {"inspect", "plan"}:
            command.add_argument(
                "--source-id",
                required=True,
                help="Stable identity of the legacy database across snapshots",
            )
            command.add_argument(
                "--profile",
                choices=["kingforge-v1", "standalone", "universal"],
                action="append",
                required=True,
                help="Repeat for coexisting profiles; all detected profiles must be declared",
            )
        if name != "inspect":
            transport = command.add_mutually_exclusive_group(required=True)
            transport.add_argument("--socket", type=Path)
            transport.add_argument("--url")
            command.add_argument("--tenant")
            command.add_argument("--ca-file", type=Path)
            command.add_argument("--credential", type=Path, required=True)
            command.add_argument("--project", required=True)
        if name == "apply":
            command.add_argument("--start", type=int, default=0)
            command.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    try:
        if args.action == "snapshot":
            freeze(args.source, args.directory)
            result: dict[str, Any] = {"snapshot_created": True, "source_modified": False}
        else:
            private_dir(args.directory)
            path = args.directory / "source.sqlite"
            private_file(path)
            snapshot_receipt = args.directory / "snapshot.json"
            private_file(snapshot_receipt)
            if load_strict(snapshot_receipt.read_bytes())["sha256"] != digest_file(path):
                raise LocalError("legacy_snapshot_hash_mismatch")
            if args.action == "inspect":
                data = summary(inspect(path, args.source_id, expected_profiles=args.profile))
                # Inspections can exceed canonical wire size. This is a private
                # local report, not an API payload or authority token.
                atomic(
                    args.directory / "inspection.json",
                    (
                        json.dumps(
                            data, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
                        )
                        + "\n"
                    ).encode(),
                )
                result = {
                    "tables": data["tables"],
                    "rows": len(data["rows"]),
                    "unresolved_count": len(data["unresolved"]),
                    "writes": 0,
                }
            else:
                if args.url:
                    if not args.tenant:
                        raise LocalError("legacy_shared_tenant_required")
                    client: LocalClient = SharedClient(
                        args.url, args.tenant, args.credential, args.project, ca_file=args.ca_file
                    )
                else:
                    client = LocalClient(args.socket, args.credential, args.project)
                operator = Operator(client)
                plan_path = args.directory / "plan.json"
                if args.action == "plan":
                    if plan_path.exists():
                        raise LocalError("legacy_plan_exists_use_new_snapshot_directory")
                    plan = make_plan(path, args.source_id, args.profile, operator)
                    atomic(plan_path, canonical_bytes(plan))
                    result = {
                        "planned_rows": plan["rows"],
                        "unresolved_count": len(plan["unresolved"]),
                        "writes": 0,
                        "authority": plan["authority"],
                    }
                else:
                    private_file(plan_path)
                    plan = load_strict(plan_path.read_bytes())
                    journal = args.directory / "journal"
                    if args.action == "apply":
                        result = apply_batch(
                            path, plan, operator, journal, start=args.start, limit=args.limit
                        )
                        name = f"batch-{args.start}.json"
                    else:
                        result = reconcile(path, plan, operator, journal)
                        name = "reconciliation.json"
                    atomic(
                        args.directory / name,
                        (
                            json.dumps(result, sort_keys=True, indent=2, allow_nan=False) + "\n"
                        ).encode(),
                    )
                    result = {k: v for k, v in result.items() if k != "mapping"}
        print(json.dumps(result, sort_keys=True))
    except (LocalError, OSError, ValueError, KeyError) as exc:
        code = exc.code if isinstance(exc, LocalError) else "legacy_input_or_transport_error"
        print(json.dumps({"error": code, "automatic_retry": False}))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
