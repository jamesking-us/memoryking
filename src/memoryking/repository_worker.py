"""One bounded queued repository comparison through the authenticated local SDK."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from memoryking.adapters.local_store import uid
from memoryking.contracts.canonical import ContractError, canonical_bytes
from memoryking.domain.local import LocalError
from memoryking.sdk import LocalClient


def run_once(client: LocalClient, repository: str) -> dict[str, object]:
    def command(action: str, args: dict[str, object], key: str | None = None) -> dict[str, object]:
        request = {
            "version": 1,
            "incarnation": client.binding[-1],
            "action": action,
            "args": {"project": client.project, "repository": repository, **args},
        }
        if key:
            request["key"] = key
        return client.repository(request)

    jobs = command("pending", {})["jobs"]
    if not isinstance(jobs, list) or not jobs:
        return {"status": "idle", "checks_executed": False}
    job = jobs[0]
    return command(
        "revalidate",
        {"snapshot": job["snapshot"], "expected_generation": job["generation"]},
        uid("revalidate"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--credential", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--once", action="store_true", required=True)
    args = parser.parse_args()
    try:
        client = LocalClient(args.socket, args.credential, args.project)
        sys.stdout.buffer.write(canonical_bytes(run_once(client, args.repository)) + b"\n")
    except (LocalError, ContractError, OSError, ValueError) as exc:
        sys.stderr.write(
            (exc.code if isinstance(exc, LocalError) else "invalid_local_input") + "\n"
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
