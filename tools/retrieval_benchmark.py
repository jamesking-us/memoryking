"""Labelled retrieval benchmark on a deterministic synthetic corpus, with a release gate.

The corpus is generated from a seed: every query is built to have a known relevant set
(documents sharing most of its distinctive terms) and distractors (documents sharing one
term). Recall, precision, reciprocal rank, briefing tokens and latency are reported.
`--gate` compares the result with the thresholds in fixtures/evaluation/retrieval-gate.json
and exits non-zero when any threshold is missed, so ranking changes are measured rather
than assumed. Synthetic text only: this is a regression gate, not a quality claim.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memoryking.adapters.local_store import LocalStore  # noqa: E402
from memoryking.adapters.retrieval import Retrieval  # noqa: E402

GATE = ROOT / "fixtures/evaluation/retrieval-gate.json"
COMMON = "service request retry timeout cache deploy release config test build log".split()
SYLLABLES = "ka to ri mu ne so fa li do ve zu pa qi ro ba te".split()


def word(rng: random.Random) -> str:
    return "".join(rng.choice(SYLLABLES) for _ in range(3))


def corpus(
    seed: int, documents: int, queries: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Documents and labelled queries. Each query has three rare terms; relevant documents
    carry two or three of them, distractors exactly one, the rest none."""
    rng = random.Random(seed)
    rare = [word(rng) for _ in range(queries * 3)]
    docs: list[dict[str, Any]] = []
    labelled: list[dict[str, Any]] = []
    for q in range(queries):
        terms = rare[q * 3 : q * 3 + 3]
        for r in range(3):
            chosen = terms if r == 0 else rng.sample(terms, 2)
            filler = " ".join(rng.choice(COMMON) for _ in range(6))
            docs.append(
                {"text": f"{filler} {' '.join(chosen)} {filler}", "query": q, "role": "relevant"}
            )
        for _ in range(2):
            filler = " ".join(rng.choice(COMMON) for _ in range(8))
            docs.append(
                {"text": f"{filler} {rng.choice(terms)} {filler}", "query": q, "role": "distractor"}
            )
        labelled.append({"query": " ".join(terms), "relevant": [], "common": rng.choice(COMMON)})
    while len(docs) < documents:
        filler = " ".join(rng.choice(COMMON) for _ in range(10))
        docs.append({"text": filler + " " + word(rng), "query": None, "role": "noise"})
    rng.shuffle(docs)
    # Relevant indices were assigned before the shuffle; recompute them by role and query.
    for q, item in enumerate(labelled):
        item["relevant"] = [
            i for i, doc in enumerate(docs) if doc["query"] == q and doc["role"] == "relevant"
        ]
    return docs, labelled


def run(seed: int, documents: int, queries: int, top_k: int) -> dict[str, Any]:
    docs, labelled = corpus(seed, documents, queries)
    recalls: list[float] = []
    precisions: list[float] = []
    reciprocal: list[float] = []
    window_recalls: list[float] = []
    tokens: list[int] = []
    latencies: list[float] = []
    with tempfile.TemporaryDirectory(prefix="mk-benchmark-", dir="/tmp") as directory:
        root = Path(directory)
        credential = LocalStore.initialize(root / "db", root / "ctl").read_text()
        with LocalStore(root / "db", root / "ctl") as store:
            namespace = store.namespace(credential, "bench")
            ids: list[str] = []
            for position, doc in enumerate(docs):
                result = store.capture(
                    credential,
                    namespace,
                    f"d{position}",
                    {
                        "version": 1,
                        "project": "bench",
                        "source": "benchmark",
                        "source_event": f"d{position}",
                        "text": doc["text"],
                        "readers": None,
                        "classification": "internal",
                        "observed_at_ms": position + 1,
                    },
                )
                ids.append(result["observation"])
            retrieval = Retrieval(store)

            def brief(query: str) -> tuple[list[str], int, float]:
                command = {
                    "version": 1,
                    "incarnation": store.incarnation,
                    "action": "brief",
                    "args": {
                        "project": "bench",
                        "query": query,
                        "bindings": [{}],
                        "max_output_tokens": 6000,
                        "tokenizer": "utf8-byte-estimate-v1",
                        "top_k": top_k,
                    },
                }
                started = time.perf_counter()
                emitted: list[bytes] = []
                retrieval.execute(credential, command).emit(emitted.append)
                elapsed = (time.perf_counter() - started) * 1000
                packet = json.loads(emitted[0])
                return (
                    [g["reference"] for g in packet["guidance"]],
                    packet["usage"]["output_tokens"],
                    elapsed,
                )

            for item in labelled:
                relevant = {ids[i] for i in item["relevant"]}
                returned, used, elapsed = brief(item["query"])
                hits = [r for r in returned if r in relevant]
                recalls.append(len(set(hits)) / len(relevant))
                precisions.append(len(hits) / len(returned) if returned else 0.0)
                rank = next((n for n, r in enumerate(returned, 1) if r in relevant), None)
                reciprocal.append(1 / rank if rank else 0.0)
                tokens.append(used)
                latencies.append(elapsed)
                # A common word widens the candidate set past the window; recent relevant
                # documents must still be found.
                returned_common, _, _ = brief(item["query"] + " " + item["common"])
                window_recalls.append(len(relevant & set(returned_common)) / len(relevant))
    return {
        "seed": seed,
        "documents": len(docs),
        "queries": len(labelled),
        "top_k": top_k,
        "recall_mean": round(statistics.fmean(recalls), 4),
        "precision_mean": round(statistics.fmean(precisions), 4),
        "mrr": round(statistics.fmean(reciprocal), 4),
        "window_recall_mean": round(statistics.fmean(window_recalls), 4),
        "tokens_mean": round(statistics.fmean(tokens), 1),
        "latency_ms_p50": round(statistics.median(latencies), 2),
        "latency_ms_p95": round(sorted(latencies)[int(len(latencies) * 0.95) - 1], 2),
    }


def gate(report: dict[str, Any], thresholds: dict[str, Any]) -> list[str]:
    failures = []
    for metric, bound in thresholds.get("minimum", {}).items():
        if report[metric] < bound:
            failures.append(f"{metric} {report[metric]} below {bound}")
    for metric, bound in thresholds.get("maximum", {}).items():
        if report[metric] > bound:
            failures.append(f"{metric} {report[metric]} above {bound}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--documents", type=int, default=400)
    parser.add_argument("--queries", type=int, default=200)
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--gate", action="store_true", help="Fail when a threshold is missed")
    args = parser.parse_args()
    report = run(args.seed, args.documents, args.queries, args.top_k)
    report["gate"] = json.loads(GATE.read_text()) if GATE.exists() else {}
    report["failures"] = gate(report, report["gate"]) if args.gate else []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k not in {"gate"}}))
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
