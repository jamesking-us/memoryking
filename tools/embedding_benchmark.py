"""Throughput, latency and memory of local embedding models on this machine's CPU.

For each installed model directory (named by its manifest id) this loads the model on one CPU
thread in a fresh process, embeds a fixed sample of real chat turns as documents (the
indexing rate) and a fixed sample of questions one at a time as queries (what a briefing
waits for), and reports documents a second, tokens a second, query latency at the median and
the 95th percentile, the model's size on disk and the process's peak memory. Pass the same
LongMemEval-format file the recall harness uses; it is only read.

    python tools/embedding_benchmark.py --data lme_oracle.json --models /tmp/mk-models \
        --model minilm-l6-v2-onnx --model bge-small-en-v1-5-onnx --output /tmp/embedding.json

The machine is shared in practice: repeat the run and read the spread (`--repeats`).
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


def sample(path: Path, documents: int, queries: int) -> tuple[list[str], list[str]]:
    data = json.loads(path.read_text())
    rng = random.Random(5)
    turns = [
        f"{t['role']}: {t['content']}"[:16384]
        for item in data
        for session in item["haystack_sessions"]
        for t in session
        if t["content"].strip()
    ]
    return rng.sample(turns, documents), [q["question"] for q in rng.sample(data, queries)]


def child(args: argparse.Namespace) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from memoryking.adapters.local_embeddings import LocalOnnxEmbedding

    documents, queries = sample(args.data, args.documents, args.queries)
    started = time.perf_counter()
    model = LocalOnnxEmbedding(args.models / args.child, args.child, threads=args.threads)
    load_seconds = time.perf_counter() - started
    model.embed(documents[:2])  # warm up
    rates = []
    tokens = 0
    started = time.perf_counter()
    # Sorted by length so a batch pads little, as the harness and a backfill do.
    ordered = sorted(documents, key=len)
    for start in range(0, len(ordered), 16):
        batch = model.embed(ordered[start : start + 16])
        tokens += batch.input_tokens or 0
    seconds = time.perf_counter() - started
    rates.append(seconds)
    latencies = []
    for question in queries:
        started = time.perf_counter()
        model.embed([question], kind="query")
        latencies.append((time.perf_counter() - started) * 1000)
    latencies.sort()
    print(
        json.dumps(
            {
                "model": args.child,
                "dimensions": model.dimensions,
                "max_input_tokens": model.limit,
                "load_seconds": round(load_seconds, 2),
                "documents_per_second": round(len(documents) / seconds, 1),
                "tokens_per_second": round(tokens / seconds),
                "query_ms_p50": round(statistics.median(latencies), 1),
                "query_ms_p95": round(latencies[max(0, int(len(latencies) * 0.95) - 1)], 1),
                "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024),
            }
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True, help="Directory of installed models")
    parser.add_argument("--model", action="append", default=[], dest="names")
    parser.add_argument("--documents", type=int, default=300)
    parser.add_argument("--queries", type=int, default=60)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        child(args)
        return
    rows = []
    for name in args.names:
        runs = []
        for _ in range(args.repeats):
            output = subprocess.run(
                [sys.executable, "-I", __file__, "--child", name, "--data", str(args.data)]
                + ["--models", str(args.models), "--documents", str(args.documents)]
                + ["--queries", str(args.queries), "--threads", str(args.threads)],
                capture_output=True,
                text=True,
                check=True,
                env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            ).stdout
            runs.append(json.loads(output.splitlines()[-1]))
        best = max(runs, key=lambda r: r["documents_per_second"])
        size = sum(f.stat().st_size for f in (args.models / name).rglob("*") if f.is_file())
        rows.append({**best, "disk_mb": round(size / 1048576), "runs": runs})
        print(json.dumps({k: v for k, v in rows[-1].items() if k != "runs"}), flush=True)
    report = {
        "machine": platform.processor() or platform.machine(),
        "cpu_count": os.cpu_count(),
        "threads": args.threads,
        "documents": args.documents,
        "queries": args.queries,
        "rows": rows,
    }
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
