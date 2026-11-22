"""Scale regression: capture, drain, briefing, status, reindex, backup and restore on a large store.

For each size N the tool builds a local store of N observations, then measures what an
operator and an agent would feel:

* capture latency (p50, p95, max) of 200 further captures;
* drain time per queued job after those captures;
* briefing latency (p50, p95, max) for a 3-term and a 20-term query;
* `status` latency, reindex (the bounded call and the whole rebuild), backup and restore.

How the store is built: every observation goes through the real capture path
(`LocalStore.capture`: contract validation, authorization, event, blob, anchors, lexical
index and statistics, projection job), in a realistic Zipf-like vocabulary of about 120
words each, in batches with a drain between them as the daemon does. No row is inserted
behind the store's back. The only shortcut is durability: while building, `fsync` and the
SQLite `synchronous` setting are off, because syncing 100,000 times only measures the disk.
Both are back on before anything is timed, so the measured captures, drains, backup and
restore run with the product's normal durability. The build rate is reported but not judged.

Budgets (`fixtures/evaluation/scale-budgets.json`) are per size, set from measurements with
margin; `--check` exits non-zero when one is exceeded, and also when any single request
(capture, drain call, briefing, status, the reindex call) takes the five-second request
deadline or longer. Backup, restore and the whole reindex are offline or background work with
their own budgets. Timings depend on the machine: the budgets are regression guards for the
host class recorded in the budget file, not performance promises.

    python tools/scale_regression.py --sizes 20000 --output tmp/scale.json --check
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import statistics
import sys
import tempfile
import time
import types
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memoryking.adapters import local_files  # noqa: E402
from memoryking.adapters.local_store import LocalStore  # noqa: E402
from memoryking.adapters.retrieval import Retrieval  # noqa: E402

BUDGETS = ROOT / "fixtures/evaluation/scale-budgets.json"
DEADLINE_MS = 5000
SAMPLE = 200  # captures measured with durability on; below the 1,000-job queue bound
COMMON = "service request retry timeout cache deploy release config test build log error fix agent task".split()
VOCAB = [f"w{i}" for i in range(4000)]


class Corpus:
    def __init__(self, seed: int = 7):
        self.rng = random.Random(seed)

    def document(self) -> str:
        """About 120 words: a third from a small common vocabulary, the rest long-tailed."""
        rng = self.rng
        words = [rng.choice(COMMON) for _ in range(30)] + [
            VOCAB[min(len(VOCAB) - 1, int(rng.paretovariate(1.1)))] for _ in range(90)
        ]
        rng.shuffle(words)
        return " ".join(words)

    def query(self, terms: int) -> str:
        """Common words and mid-frequency ones, as an agent's task description would have."""
        rng = self.rng
        common = rng.sample(COMMON, max(1, terms // 2))
        rest = [VOCAB[rng.randrange(1, 400)] for _ in range(terms - len(common))]
        return " ".join(common + rest)


@contextmanager
def without_durability(store: LocalStore) -> Iterator[None]:
    """Turn fsync and SQLite's synchronous setting off for the build, then back on."""
    real_fsync, real_fcntl = os.fsync, local_files.fcntl
    os.fsync = lambda fd: None  # type: ignore[assignment]
    local_files.fcntl = types.SimpleNamespace()  # type: ignore[assignment]  # no F_FULLFSYNC
    store.db.execute("PRAGMA synchronous=OFF")
    store.db.execute("PRAGMA fullfsync=OFF")
    store.db.execute("PRAGMA checkpoint_fullfsync=OFF")
    try:
        yield
    finally:
        os.fsync, local_files.fcntl = real_fsync, real_fcntl
        store.db.execute("PRAGMA synchronous=FULL")
        store.db.execute("PRAGMA fullfsync=ON")
        store.db.execute("PRAGMA checkpoint_fullfsync=ON")
        store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def observation(project: str, number: int, text: str) -> dict[str, Any]:
    return {
        "version": 1,
        "project": project,
        "source": "scale",
        "source_event": f"d{number}",
        "text": text,
        "readers": None,
        "classification": "internal",
        "observed_at_ms": number + 1,
    }


def spread(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "p50": round(statistics.median(ordered), 2),
        "p95": round(ordered[max(0, int(len(ordered) * 0.95) - 1)], 2),
        "max": round(ordered[-1], 2),
    }


def directory_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def measure(size: int, progress: bool = True) -> dict[str, Any]:
    corpus = Corpus()
    project = "scale"
    result: dict[str, Any] = {"observations": size}
    with tempfile.TemporaryDirectory(prefix="mk-scale-", dir="/tmp") as directory:
        base = Path(directory)
        data, control = base / "db", base / "ctl"
        credential = LocalStore.initialize(data, control).read_text()
        store = LocalStore(data, control)
        try:
            namespace = store.namespace(credential, project)
            started = time.perf_counter()
            with without_durability(store):
                for number in range(size - SAMPLE):
                    store.capture(
                        credential,
                        namespace,
                        f"d{number}",
                        observation(project, number, corpus.document()),
                    )
                    if number % 200 == 199:
                        while store.drain():
                            pass
                    if progress and number % 10000 == 9999:
                        print(f"  built {number + 1}/{size}", file=sys.stderr, flush=True)
                while store.drain():
                    pass
            result["build_seconds"] = round(time.perf_counter() - started, 1)

            captures = []
            for number in range(size - SAMPLE, size):
                started = time.perf_counter()
                store.capture(
                    credential,
                    namespace,
                    f"d{number}",
                    observation(project, number, corpus.document()),
                )
                captures.append((time.perf_counter() - started) * 1000)
            result["capture_ms"] = spread(captures)

            jobs = store.db.execute("SELECT count(*) FROM jobs WHERE state<>'complete'").fetchone()[
                0
            ]
            calls = []
            started = time.perf_counter()
            while True:
                call = time.perf_counter()
                done = store.drain()
                calls.append((time.perf_counter() - call) * 1000)
                if not done:
                    break
            drain_total = (time.perf_counter() - started) * 1000
            result["drain"] = {
                "jobs": jobs,
                "ms_per_job": round(drain_total / max(1, jobs), 3),
                "call_ms_max": round(max(calls), 2),
            }

            retrieval = Retrieval(store)

            def brief(query: str) -> float:
                command = {
                    "version": 1,
                    "incarnation": store.incarnation,
                    "action": "brief",
                    "args": {
                        "project": project,
                        "query": query,
                        "bindings": [{}],
                        "max_output_tokens": 6000,
                        "tokenizer": "utf8-byte-estimate-v1",
                        "top_k": 8,
                    },
                }
                out: list[bytes] = []
                started = time.perf_counter()
                retrieval.execute(credential, command).emit(out.append)
                return (time.perf_counter() - started) * 1000

            for terms in (3, 20):
                brief(corpus.query(terms))  # warm the caches once
                result[f"brief_{terms}_terms_ms"] = spread(
                    [brief(corpus.query(terms)) for _ in range(30)]
                )

            statuses = []
            for _ in range(5):
                started = time.perf_counter()
                status = store.status(credential, project)
                statuses.append((time.perf_counter() - started) * 1000)
            result["status_ms"] = spread(statuses)
            result["lexical_state"] = status["lexical"].get("state")

            started = time.perf_counter()
            store.reindex(credential)
            first_call = (time.perf_counter() - started) * 1000
            rebuild_calls = [first_call]
            while True:
                call = time.perf_counter()
                finished = store.rebuild_lexical(wall_ms=1500)
                rebuild_calls.append((time.perf_counter() - call) * 1000)
                if finished:
                    break
            result["reindex"] = {
                "first_call_ms": round(first_call, 2),
                "call_ms_max": round(max(rebuild_calls), 2),
                "total_seconds": round(time.perf_counter() - started, 2),
            }
            result["brief_3_terms_after_reindex_ms"] = round(brief(corpus.query(3)), 2)

            archive = base / "backup"
            started = time.perf_counter()
            store.backup(archive)
            result["backup"] = {
                "seconds": round(time.perf_counter() - started, 2),
                "bytes": directory_bytes(archive),
            }
            store.close()
            started = time.perf_counter()
            credential = LocalStore.restore(data, control, archive).read_text()
            restore_seconds = time.perf_counter() - started
            store = LocalStore(data, control)
            retrieval = Retrieval(store)
            result["restore"] = {"seconds": round(restore_seconds, 2)}
            result["brief_3_terms_after_restore_ms"] = round(brief(corpus.query(3)), 2)
            result["store_bytes"] = directory_bytes(data)
        finally:
            store.close()
    return result


def flatten(report: dict[str, Any]) -> dict[str, float]:
    """The judged figures, by name."""
    return {
        "capture_ms_p95": report["capture_ms"]["p95"],
        "drain_ms_per_job": report["drain"]["ms_per_job"],
        "brief_3_terms_ms_p50": report["brief_3_terms_ms"]["p50"],
        "brief_3_terms_ms_p95": report["brief_3_terms_ms"]["p95"],
        "brief_20_terms_ms_p50": report["brief_20_terms_ms"]["p50"],
        "brief_20_terms_ms_p95": report["brief_20_terms_ms"]["p95"],
        "status_ms_p95": report["status_ms"]["p95"],
        "reindex_total_seconds": report["reindex"]["total_seconds"],
        "backup_seconds": report["backup"]["seconds"],
        "restore_seconds": report["restore"]["seconds"],
    }


def requests(report: dict[str, Any]) -> dict[str, float]:
    """The slowest single request of each kind, for the deadline."""
    return {
        "capture": report["capture_ms"]["max"],
        "drain call": report["drain"]["call_ms_max"],
        "brief (3 terms)": report["brief_3_terms_ms"]["max"],
        "brief (20 terms)": report["brief_20_terms_ms"]["max"],
        "status": report["status_ms"]["max"],
        "reindex call": report["reindex"]["call_ms_max"],
        "brief after reindex": report["brief_3_terms_after_reindex_ms"],
        "brief after restore": report["brief_3_terms_after_restore_ms"],
    }


def check(report: dict[str, Any], budgets: dict[str, Any]) -> list[str]:
    failures = []
    for name, milliseconds in requests(report).items():
        if milliseconds >= DEADLINE_MS:
            failures.append(f"{name} took {milliseconds} ms, the request deadline is {DEADLINE_MS}")
    limits = budgets.get("sizes", {}).get(str(report["observations"]))
    if limits is None:
        failures.append(f"no budget recorded for {report['observations']} observations")
        return failures
    for name, value in flatten(report).items():
        if name not in limits:
            failures.append(f"no budget for {name}")
        elif value > limits[name]:
            failures.append(f"{name} {value} above budget {limits[name]}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="20000", help="Comma-separated observation counts")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="Fail past a budget or the deadline")
    args = parser.parse_args()
    budgets = json.loads(BUDGETS.read_text()) if BUDGETS.exists() else {}
    runs = []
    failures: list[str] = []
    for size in (int(s) for s in args.sizes.split(",")):
        print(f"scale: {size} observations", file=sys.stderr, flush=True)
        report = measure(size)
        report["figures"] = flatten(report)
        report["slowest_requests_ms"] = requests(report)
        report["failures"] = check(report, budgets) if args.check else []
        failures += [f"{size}: {line}" for line in report["failures"]]
        runs.append(report)
        print(json.dumps(report), flush=True)
    document = {
        "protocol": "scale_regression_v1: observations built through the real capture path "
        "with fsync and SQLite synchronous off, durability on for every timed step",
        "deadline_ms": DEADLINE_MS,
        "host": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpus": os.cpu_count(),
        },
        "runs": runs,
        "failures": failures,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2) + "\n")
    for line in failures:
        print("FAIL " + line, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
