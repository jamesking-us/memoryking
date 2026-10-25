#!/usr/bin/env python3
"""Small synthetic local measurement; never a production-envelope acceptance test."""

import argparse
import concurrent.futures
import json
import platform
import sqlite3
import statistics
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from memoryking.adapters.local_store import LocalStore


def distribution(values):
    ordered = sorted(values)
    return {
        "n": len(values),
        "p50_ms": round(statistics.median(values), 3),
        "p95_ms": round(ordered[int(0.95 * (len(ordered) - 1))], 3),
        "p99_ms": round(ordered[int(0.99 * (len(ordered) - 1))], 3),
        "max_ms": round(max(values), 3),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--captures", type=int, default=200)
    args = parser.parse_args()
    if not 10 <= args.captures <= 900:
        parser.error("captures must be 10..900")
    with tempfile.TemporaryDirectory(prefix="mk-measure-", dir="/tmp") as name:
        base = Path(name)
        root, control = base / "db", base / "control"
        credential = LocalStore.initialize(root, control).read_text()
        with LocalStore(root, control) as store:
            namespace = store.namespace(credential, "demo")

            def capture(number):
                start = time.perf_counter()
                store.capture(
                    credential,
                    namespace,
                    f"k{number}",
                    {
                        "version": 1,
                        "project": "demo",
                        "source": "measurement",
                        "source_event": f"e{number}",
                        "text": "Synthetic memory. " * 240,
                        "readers": None,
                        "classification": "internal",
                        "observed_at_ms": 0,
                    },
                )
                return (time.perf_counter() - start) * 1000

            started = time.perf_counter()
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                captures = list(pool.map(capture, range(args.captures)))
            total = time.perf_counter() - started
            reads = []
            for _ in range(30):
                started = time.perf_counter()
                store.read(credential, "demo", query="Synthetic").emit(lambda _: None)
                reads.append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            store.backup(base / "backup")
            backup_ms = (time.perf_counter() - started) * 1000
            before_incarnation = store.incarnation
        started = time.perf_counter()
        LocalStore.restore(root, control, base / "backup")
        with LocalStore(root, control) as store:
            restored = store.incarnation != before_incarnation
            retained = store.db.execute("SELECT count(*) FROM observations").fetchone()[0]
        restore_ms = (time.perf_counter() - started) * 1000
        sizes = {
            kind: sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())
            for kind, directory in {
                "content_bytes": root,
                "control_bytes": control,
                "backup_bytes": base / "backup",
            }.items()
        }
    report = {
        "classification": "synthetic_development",
        "at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "workload": {
            "tenants": 1,
            "concurrent_library_writers": 4,
            "captures": args.captures,
            "payload_bytes": len(("Synthetic memory. " * 240).encode()),
            "distinct_blob_contents": 1,
            "read_samples": 30,
            "read_scope": "one authorized project, literal FTS query, bounded first page",
            "database_start": "fresh; all capture samples included; reads warm",
            "transport": "in-process adapter, socket measured separately by functional rehearsal",
        },
        "capture": distribution(captures),
        "capture_throughput_per_second": round(args.captures / total, 3),
        "read": distribution(reads),
        "backup_ms": round(backup_ms, 3),
        "restore_reopen_ms": round(restore_ms, 3),
        "restored_new_history": restored,
        "retained_observations": retained,
        **sizes,
        "limitations": [
            "small deduplicating synthetic corpus",
            "no real-task efficacy or token savings",
            "no confidence interval or production target qualification",
            "no shared backend, power failure, disk loss or sustained contention measurement",
            "assistant usage unmetered; product made no model calls",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
