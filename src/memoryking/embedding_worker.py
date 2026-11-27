"""One bounded local CPU embedding attempt through the authenticated SDK."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from time import monotonic, time
from typing import Any

from memoryking.adapters.local_embeddings import LocalOnnxEmbedding
from memoryking.contracts.canonical import canonical_hash
from memoryking.domain.local import LocalError
from memoryking.sdk import LocalClient
from memoryking.shared_sdk import SharedClient


def run_once(client: LocalClient, provider: LocalOnnxEmbedding) -> dict[str, Any]:
    """Load the model before calling. Never dispatch twice on one release/lease.

    Authorization is evaluated by the server at claim, with a five-second
    start deadline. Local CPU inference may finish afterward; commit checks the
    current source, permissions, generation and 30-second lease again.
    """
    if type(provider) is not LocalOnnxEmbedding:
        raise LocalError("embedding_provider_unsupported")

    def command(action: str, **args: Any) -> dict[str, Any]:
        return client.embedding(
            {
                "version": 1,
                "incarnation": client.binding[-1],
                "action": action,
                "args": {"project": client.project, **args},
            }
        )

    start, utc_start = monotonic(), time()
    job = command("claim", fingerprint=provider.fingerprint)
    if job["job"] is None:
        return {"status": "idle", "frontier": job["frontier"], "ready": job["ready"]}
    input_tokens: int | None = 0
    try:
        if (
            job["fingerprint"] != provider.fingerprint
            or job["dimensions"] != provider.dimensions
            or job["input_hash"] != canonical_hash(job["input"])
            or job["egress"] != "local_cpu_only"
        ):
            raise LocalError("embedding_release_invalid")
        elapsed, utc = monotonic() - start, time()
        if elapsed < 0 or utc < utc_start or utc * 1000 < job["evaluated_ms"]:
            raise LocalError("clock_untrusted")
        if abs((utc - utc_start) - elapsed) > 1:
            raise LocalError("clock_untrusted")
        if elapsed >= 5 or utc * 1000 >= job["deadline_ms"]:
            raise LocalError("embedding_dispatch_expired")
        # Unknown once inference starts: an exception is never zero token usage.
        input_tokens = None
        batch = provider.embed([job["input"]])
        batch.validate(provider.fingerprint, provider.dimensions, [job["input"]])
        input_tokens = batch.input_tokens
    except Exception as exc:
        reason = "provider_failure" if input_tokens is None else "interrupted"
        try:
            result = command(
                "fail",
                lease=job["lease"],
                reason=reason,
                input_tokens=input_tokens,
                duration_ms=min(600000, int((monotonic() - start) * 1000)),
            )
        except LocalError as failure:
            return {"status": "reconciliation_required", "error": failure.code}
        return {
            "status": "attempt_failed",
            "job_status": result["status"],
            "error": exc.code if isinstance(exc, LocalError) else "embedding_provider_failure",
            "usage_attempt": result["usage_attempt"],
        }
    # An ambiguous completion is not a reason to run inference or mutate again.
    # Its durable started usage remains unknown until explicitly reconciled.
    try:
        return command(
            "complete",
            lease=job["lease"],
            fingerprint=batch.fingerprint,
            input_hash=batch.input_hashes[0],
            vector=batch.vectors[0],
            input_tokens=input_tokens,
            truncated=batch.truncated[0],
            duration_ms=min(600000, int((monotonic() - start) * 1000)),
        )
    except LocalError as exc:
        return {"status": "reconciliation_required", "error": exc.code}


def run_batch(client: LocalClient, provider: LocalOnnxEmbedding, count: int = 16) -> dict[str, Any]:
    """Up to `count` jobs (at most 16) in one claim, one model pass and one completion.

    Each job is checked, leased and stored exactly as `run_once` would: the batch saves
    round trips and lets the model see several texts at once, nothing else. A job that fails
    its checks, or a model failure, is failed on its own; a lease that has gone stale (the
    record was purged or changed meanwhile) is refused by the server and stores no vector."""
    if type(provider) is not LocalOnnxEmbedding:
        raise LocalError("embedding_provider_unsupported")

    def command(action: str, **args: Any) -> dict[str, Any]:
        return client.embedding(
            {
                "version": 1,
                "incarnation": client.binding[-1],
                "action": action,
                "args": {"project": client.project, **args},
            }
        )

    start, utc_start = monotonic(), time()
    reply = command("claim_batch", fingerprint=provider.fingerprint, count=min(16, count))
    jobs = reply["jobs"]
    if not jobs:
        return {"status": "idle", "frontier": reply["frontier"], "ready": reply["ready"]}
    failed = 0

    def fail(job: dict[str, Any], reason: str, tokens: int | None) -> None:
        nonlocal failed
        failed += 1
        try:
            command(
                "fail",
                lease=job["lease"],
                reason=reason,
                input_tokens=tokens,
                duration_ms=min(600000, int((monotonic() - start) * 1000)),
            )
        except LocalError:
            pass  # the lease expires on its own and the job is retried

    elapsed, utc = monotonic() - start, time()
    expired = (
        elapsed < 0
        or utc < utc_start
        or utc * 1000 < reply["evaluated_ms"]
        or abs((utc - utc_start) - elapsed) > 1
        or elapsed >= 5
        or utc * 1000 >= reply["deadline_ms"]
    )
    ready = []
    for job in jobs:
        if (
            expired
            or job["fingerprint"] != provider.fingerprint
            or job["dimensions"] != provider.dimensions
            or job["input_hash"] != canonical_hash(job["input"])
            or job["egress"] != "local_cpu_only"
        ):
            fail(job, "interrupted", 0)
        else:
            ready.append(job)
    if not ready:
        return {"status": "attempt_failed", "count": 0, "failed": failed}
    try:
        batch = provider.embed([job["input"] for job in ready])
        batch.validate(provider.fingerprint, provider.dimensions, [job["input"] for job in ready])
    except Exception:
        for job in ready:
            fail(job, "provider_failure", None)
        return {"status": "attempt_failed", "count": 0, "failed": failed}
    each = min(600000, int((monotonic() - start) * 1000 / len(ready)))
    counts: list[int | None] = [None] * len(ready)
    if batch.input_token_counts:
        counts = list(batch.input_token_counts)
    try:
        outcome = command(
            "complete_batch",
            items=[
                {
                    "lease": job["lease"],
                    "fingerprint": batch.fingerprint,
                    "input_hash": batch.input_hashes[n],
                    "vector": batch.vectors[n],
                    "input_tokens": counts[n],
                    "truncated": batch.truncated[n],
                    "duration_ms": each,
                }
                for n, job in enumerate(ready)
            ],
        )
    except LocalError as exc:
        return {"status": "reconciliation_required", "error": exc.code}
    stored = sum(1 for item in outcome["results"] if item["status"] == "complete")
    return {
        "status": "complete",
        "count": len(jobs),
        "stored": stored,
        "refused": len(ready) - stored,
        "failed": failed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--socket", type=Path)
    source.add_argument("--shared-url")
    parser.add_argument("--tenant")
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--credential", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument(
        "--count", type=int, default=16, help="Records per model call, 1 to 16 (default 16)"
    )
    parser.add_argument(
        "--until-idle", action="store_true", help="Keep going until no record is left to index"
    )
    args = parser.parse_args()
    if args.shared_url and not args.tenant:
        parser.error("--tenant is required with --shared-url")
    try:
        from memoryking import semantic

        provider = LocalOnnxEmbedding(args.model, semantic.name_of(args.model))
        client = (
            SharedClient(
                args.shared_url,
                args.tenant,
                args.credential,
                args.project,
                ca_file=args.ca_file,
            )
            if args.shared_url
            else LocalClient(args.socket, args.credential, args.project)
        )
        while True:
            result = run_batch(client, provider, args.count)
            print(json.dumps(result, sort_keys=True), flush=True)
            if not args.until_idle or result["status"] in {"idle", "reconciliation_required"}:
                break
    except LocalError as exc:
        print(json.dumps({"error": exc.code}))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
