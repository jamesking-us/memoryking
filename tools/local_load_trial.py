#!/usr/bin/env python3
"""Bounded local corpus/load/recovery trial using real socket clients and storage.

This measures an available host, not the shared reference hardware or a real-task
study. Seed rows are episodes (one retrieval unit each); no claim of a separately
compressed 10,000-unit / 100,000-occurrence corpus or 10 GiB payload is made.
"""

import argparse
import concurrent.futures
import json
import math
import os
import platform
import shutil
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from memoryking.adapters.local_files import atomic
from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
from memoryking.domain.local import LocalError
from memoryking.harness_context import AuthorizedConversation, LocalRecordingProvider
from memoryking.sdk import LocalClient

ROOT = Path(__file__).resolve().parents[1]


def observation(key):
    return {
        "version": 1,
        "project": "trial",
        "source": "synthetic-load",
        "source_event": key,
        "text": f"Observation {key}: timeout evidence, keep verification gaps. "
        + "Synthetic bounded corpus. " * 150,
        "readers": None,
        "classification": "internal",
        "observed_at_ms": 0,
    }


def distribution(rows):
    values = sorted(rows)
    return {
        "n": len(values),
        **{
            f"p{p}_ms": round(values[max(0, math.ceil(len(values) * p / 100) - 1)], 3)
            if values
            else None
            for p in (50, 95, 99)
        },
    }


def run(args):
    args.directory.mkdir(parents=True, mode=0o700)
    os.chmod(args.directory, 0o700)
    data, control = args.directory / "data", args.directory / "control"
    output = args.output
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    result = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "source_revision": args.revision,
        "classification": "synthetic_local_trial",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "logical_cpus": os.cpu_count(),
        "initial_disk_free_bytes": shutil.disk_usage(args.directory).free,
        "requested": {
            "seed_episodes": args.seed,
            "seed_deadline_seconds": args.seed_deadline,
            "warmup_seconds": args.warmup,
            "duration_seconds": args.duration,
            "socket_clients": 4,
            "briefs_per_second": 1,
            "mutations_per_second": 2,
        },
        "release_ready": False,
        "reference_envelope_qualified": False,
    }
    records, seed_count, backfill_count = [], 0, 0
    store = None
    try:
        if shutil.disk_usage(args.directory).free < 8 * 1024**3:
            raise LocalError("trial_disk_reserve")
        credential_path = LocalStore.initialize(data, control)
        owner = credential_path.read_text()
        store = LocalStore(data, control)
        namespace = store.namespace(owner, "trial")
        first = None
        for index in range(args.seed):
            if time.monotonic() - started > args.seed_deadline:
                raise LocalError("trial_seed_deadline")
            key = f"seed-{index}"
            captured = store.capture(owner, namespace, key, observation(key))
            if first is None:
                first = captured
            seed_count += 1
            # Drain bounded batches through actual claim/finish, retaining a backlog
            # for the measured interval; no direct database writes seed the corpus.
            if index % 64 == 63:
                for _ in range(64):
                    job = store.claim(owner, "trial")
                    if job is None:
                        break
                    store.finish(owner, "trial", job["lease"], "complete")
                    backfill_count += 1
            if index % 1000 == 999:
                progress = {
                    "seeded": seed_count,
                    "backfill_completed": backfill_count,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
                (output / "seed-progress.json").write_text(json.dumps(progress, indent=2) + "\n")
                print(json.dumps(progress), flush=True)
                if shutil.disk_usage(args.directory).free < 8 * 1024**3:
                    raise LocalError("trial_disk_reserve")
        result["seed_seconds"] = round(time.monotonic() - started, 3)
        paths = []
        for i in range(4):
            credential = store.grant(
                owner, f"agent{i}", "trial", ["read", "capture", "checkpoint", "inference"]
            )
            path = control / f"agent{i}.credential"
            atomic(path, credential.encode())
            paths.append(path)
        checkpoint = json.loads((ROOT / "fixtures/contracts/checkpoint.json").read_text())
        checkpoint["objective"] = "Synthetic local load checkpoint; no task-quality claim"
        # Reindex schedules a real bounded backlog. Canonical capture/search remains
        # authoritative while this optional projection catches up.
        store.reindex(owner)
        stop = threading.Event()
        worker_errors = Counter()

        def worker():
            nonlocal backfill_count
            while not stop.is_set():
                try:
                    job = store.claim(owner, "trial")
                    if job:
                        store.finish(owner, "trial", job["lease"], "complete")
                        backfill_count += 1
                    else:
                        stop.wait(0.05)
                except LocalError as exc:
                    worker_errors[exc.code] += 1
                    stop.wait(0.05)

        with Server(store) as server:
            serving = threading.Thread(target=server.serve_forever, daemon=True)
            serving.start()
            clients = [LocalClient(server.path, path, "trial") for path in paths]
            provider = LocalRecordingProvider(args.directory / "provider")
            background = threading.Thread(target=worker, daemon=True)
            background.start()
            epoch = time.monotonic()

            def client_loop(number):
                client = clients[number]
                local = []
                # Four fixed clients partition the aggregate three requests/second.
                for index in range(number, math.ceil((args.warmup + args.duration) * 3), 4):
                    scheduled = epoch + index / 3
                    stop.wait(max(0, scheduled - time.monotonic()))
                    if stop.is_set():
                        break
                    begin = time.monotonic()
                    if begin >= epoch + args.warmup + args.duration:
                        break
                    operation = ("brief", "capture", "checkpoint")[index % 3]
                    row = {
                        "client": number,
                        "operation": operation,
                        "condition": "warmup" if index / 3 < args.warmup else "measured",
                        "schedule_lag_ms": round((begin - scheduled) * 1000, 3),
                        "status": "passed",
                    }
                    try:
                        key = f"load-{index}"
                        if operation == "capture":
                            client.capture(key, observation(key))
                        elif operation == "checkpoint":
                            reply = client.working(
                                {
                                    "version": 1,
                                    "incarnation": store.incarnation,
                                    "action": "save",
                                    "key": key,
                                    "args": {
                                        "project": "trial",
                                        "task": key,
                                        "run": None,
                                        "expected_revision": 0,
                                        "readers": [f"agent{number}"],
                                        "checkpoint": checkpoint,
                                    },
                                }
                            )
                            if reply.get("status") != "durable":
                                raise LocalError("trial_checkpoint_not_durable")
                        else:
                            packet = client.retrieval(
                                {
                                    "version": 1,
                                    "incarnation": store.incarnation,
                                    "action": "brief",
                                    "args": {
                                        "project": "trial",
                                        "query": "timeout evidence",
                                        "bindings": [{}],
                                        "max_output_tokens": 1500,
                                        "tokenizer": "cl100k_base",
                                    },
                                }
                            )
                            row["brief_status"] = packet["status"]
                            row["retrieval_ms"] = round((time.monotonic() - begin) * 1000, 3)
                            conversation = AuthorizedConversation(client, provider)
                            conversation.add_host(
                                "Inspect this synthetic evidence. Do not execute tools.",
                                authorization_id=key,
                                classification="internal",
                                expires_ms=store.clock.utc_ms() + 60000,
                                approved=True,
                            )
                            conversation.add_memory(packet)
                            conversation.dispatch()
                    except LocalError as exc:
                        row["status"] = exc.code
                    except Exception as exc:
                        row["status"] = "unexpected:" + type(exc).__name__
                    row["elapsed_ms"] = round((time.monotonic() - begin) * 1000, 3)
                    local.append(row)
                    if len(local) % 50 == 0:
                        (output / f"client-{number}-progress.json").write_text(
                            json.dumps(
                                {
                                    "completed": len(local),
                                    "elapsed_seconds": round(time.monotonic() - epoch, 3),
                                    "statuses": dict(Counter(r["status"] for r in local)),
                                },
                                indent=2,
                            )
                            + "\n"
                        )
                return local

            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    for rows in pool.map(client_loop, range(4)):
                        records.extend(rows)
            finally:
                stop.set()
                background.join()
                server.shutdown()
                serving.join()
            result["load_wall_seconds"] = round(time.monotonic() - epoch, 3)
        result["worker_errors"] = dict(worker_errors)
        measured = [row for row in records if row["condition"] == "measured"]
        result["statuses"] = dict(Counter(row["status"] for row in measured))
        result["measured_requests"] = len(measured)
        result["requested_measured_requests"] = args.duration * 3
        result["completed_per_second"] = round(
            len(measured) / max(args.duration, result["load_wall_seconds"] - args.warmup), 6
        )
        result["missed_scheduled_requests"] = max(0, args.duration * 3 - len(measured))
        result["latency"] = {
            op: distribution(
                [
                    row.get("retrieval_ms", row["elapsed_ms"])
                    for row in measured
                    if row["operation"] == op and row["status"] == "passed"
                ]
            )
            for op in ("capture", "checkpoint", "brief")
        }
        result["schedule_lag"] = distribution([row["schedule_lag_ms"] for row in measured])
        result["service_gates"] = {
            op: "passed"
            if result["latency"][op]["n"] and result["latency"][op]["p95_ms"] <= limit
            else "failed"
            for op, limit in (("capture", 250), ("checkpoint", 250), ("brief", 2000))
        }
        result["service_gates"]["offered_load_completed"] = (
            "passed"
            if result["missed_scheduled_requests"] == 0
            and all(r["status"] == "passed" for r in measured)
            else "failed"
        )
        result["observations_before_backup"] = store.db.execute(
            "SELECT count(*) FROM observations"
        ).fetchone()[0]
        before = time.monotonic()
        store.backup(args.directory / "archive")
        result["backup_ms"] = round((time.monotonic() - before) * 1000, 3)
        old_history = store.incarnation
        store.purge(owner, first["observation"])
        store.close()
        store = None
        before = time.monotonic()
        LocalStore.restore(data, control, args.directory / "archive")
        store = LocalStore(data, control)
        result["restore_ms"] = round((time.monotonic() - before) * 1000, 3)
        result["restore_fresh_history"] = old_history != store.incarnation
        with store.transaction():
            result["later_purge_replayed"] = (
                store.db.execute(
                    "SELECT deleted FROM observations WHERE id=?", (first["observation"],)
                ).fetchone()[0]
                == 1
            )
            result["observations_after_restore"] = store.db.execute(
                "SELECT count(*) FROM observations"
            ).fetchone()[0]
        if (
            not result["restore_fresh_history"]
            or not result["later_purge_replayed"]
            or result["observations_after_restore"] != result["observations_before_backup"]
        ):
            raise LocalError("trial_recovery_invariant_failed")
        result["trial_finished"] = True
    except LocalError as exc:
        result["trial_finished"] = False
        result["blocked_at"] = exc.code
    except Exception as exc:
        result["trial_finished"] = False
        result["blocked_at"] = "unexpected:" + type(exc).__name__
        raise
    finally:
        if store:
            store.close()
        result["seeded_episodes"] = seed_count
        result["projection_jobs_completed"] = backfill_count
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        result["retained_state_bytes"] = sum(
            p.stat().st_size for p in args.directory.rglob("*") if p.is_file()
        )
        result["limits"] = [
            "Synthetic corpus with one indexed episode per occurrence; actual byte scale is recorded, not assumed 10 GiB.",
            "Available macOS host, no dedicated CPU/RAM reservation or shared reference hardware.",
            "Brief latency excludes the subsequent local provider dispatch, which is retained in request elapsed time.",
            "In-process server and store reopen/restore; separate launchd/Compose tests establish actual process crashes.",
            "No full-envelope qualification or quality/savings result is implied by finishing this trial.",
        ]
        (output / "samples.json").write_text(json.dumps(records, indent=2) + "\n")
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {k: result[k] for k in ("trial_finished", "seeded_episodes", "elapsed_seconds")}
        ),
        flush=True,
    )
    return 0 if result.get("trial_finished") else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--seed", type=int, default=100000)
    parser.add_argument("--seed-deadline", type=int, default=1800)
    parser.add_argument("--duration", type=int, default=1800)
    parser.add_argument("--warmup", type=int, default=30)
    args = parser.parse_args()
    if args.directory.exists() or args.output.exists():
        parser.error("choose new state and evidence directories; previous attempts are retained")
    if (
        not 64 <= args.seed <= 100000
        or not 10 <= args.duration <= 1800
        or not 0 <= args.warmup <= 300
        or not 10 <= args.seed_deadline <= 3600
    ):
        parser.error("trial bounds exceeded")
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
