"""Added latency of a local cross-encoder per briefing at windows of 16, 32 and 64 candidates.

For each installed reranker this loads the model in a fresh process on the given number of
CPU threads, then times `score()` for a real question against a window of real chat turns
(cut to the 4,096-byte excerpt a briefing would hand it) at each window size, over a sample of
questions, and reports the median and the 95th percentile of the milliseconds one briefing
would wait, the model's size on disk and the process's peak memory. The candidates are chat
turns from the question's own haystack, not the ranked top of a briefing: the cost depends on
how many pairs and how long they are, not on which ones.

    python tools/rerank_benchmark.py --data lme_oracle.json --models /tmp/mk-models \
        --model ms-marco-minilm-l6-v2-onnx --model gte-reranker-modernbert-base-onnx
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = (16, 32, 64)


def child(args: argparse.Namespace) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from memoryking.adapters.local_embeddings import LocalOnnxReranker

    rng = random.Random(9)
    data = json.loads(args.data.read_text())
    questions = rng.sample(data, args.questions)
    started = time.perf_counter()
    model = LocalOnnxReranker(args.models / args.child, args.child, threads=args.threads)
    load_seconds = time.perf_counter() - started
    rows = {}
    for window in WINDOWS:
        times = []
        for item in questions:
            turns = [
                f"{t['role']}: {t['content']}".encode()[:4096].decode("utf-8", "ignore")
                for session in item["haystack_sessions"]
                for t in session
                if t["content"].strip()
            ]
            texts = rng.sample(turns, min(window, len(turns)))
            started = time.perf_counter()
            model.score(item["question"][:2048], texts)
            times.append((time.perf_counter() - started) * 1000)
        times.sort()
        rows[str(window)] = {
            "p50_ms": round(statistics.median(times)),
            "p95_ms": round(times[max(0, int(len(times) * 0.95) - 1)]),
        }
    print(
        json.dumps(
            {
                "model": args.child,
                "threads": args.threads,
                "load_seconds": round(load_seconds, 2),
                "windows": rows,
                "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024),
            }
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--model", action="append", default=[], dest="names")
    parser.add_argument("--questions", type=int, default=30)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        child(args)
        return
    rows = []
    for name in args.names:
        output = subprocess.run(
            [sys.executable, "-I", __file__, "--child", name, "--data", str(args.data)]
            + ["--models", str(args.models), "--questions", str(args.questions)]
            + ["--threads", str(args.threads)],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        ).stdout
        row = json.loads(output.splitlines()[-1])
        size = sum(f.stat().st_size for f in (args.models / name).rglob("*") if f.is_file())
        rows.append({**row, "disk_mb": round(size / 1048576)})
        print(json.dumps(rows[-1]), flush=True)
    if args.output:
        report = {"machine": platform.machine(), "cpu_count": os.cpu_count(), "rows": rows}
        args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
