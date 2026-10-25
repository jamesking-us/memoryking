"""Small synthetic local latency/budget measurement, not release-scale or quality evidence."""

import argparse
import json
import math
import platform
import statistics
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from memoryking.adapters.local_store import LocalStore
from memoryking.adapters.retrieval import Retrieval


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = []
    started = datetime.now(timezone.utc).isoformat()
    with tempfile.TemporaryDirectory(prefix="mk-measure-brief-", dir="/tmp") as directory:
        root = Path(directory)
        credential = LocalStore.initialize(root / "db", root / "ctl").read_text()
        with LocalStore(root / "db", root / "ctl") as store:
            namespace = store.namespace(credential, "measure")
            for i in range(64):
                store.capture(
                    credential,
                    namespace,
                    f"k{i}",
                    {
                        "version": 1,
                        "project": "measure",
                        "source": "measurement",
                        "source_event": str(i),
                        "readers": None,
                        "text": (
                            "Retry timeout needs evidence and reconciliation. "
                            if i % 4 == 0
                            else "Independent documentation observation. "
                        )
                        * 8,
                        "classification": "internal",
                        "observed_at_ms": i,
                    },
                )
            request = {
                "version": 1,
                "incarnation": store.incarnation,
                "action": "brief",
                "args": {
                    "project": "measure",
                    "query": "retry timeout",
                    "bindings": [{}],
                    "max_output_tokens": 1500,
                    "tokenizer": "cl100k_base",
                    "top_k": 8,
                },
            }
            for condition, repetitions in [
                ("cold_tokenizer_and_candidate_cache", 1),
                ("warm", 30),
                ("candidate_cache_cleared", 10),
            ]:
                for _ in range(repetitions):
                    if condition == "candidate_cache_cleared":
                        store.retrieval_cache.clear()
                    before = time.perf_counter_ns()
                    result = Retrieval(store).execute(credential, request)
                    emitted = []
                    result.emit(emitted.append)
                    elapsed = (time.perf_counter_ns() - before) / 1000000
                    value = json.loads(emitted[0])
                    detail = json.loads(
                        Retrieval(store)
                        .execute(
                            credential,
                            {
                                "version": 1,
                                "incarnation": store.incarnation,
                                "action": "receipt",
                                "args": {"project": "measure", "receipt": value["receipt"]},
                            },
                        )
                        .body
                    )["diagnostics"]
                    rows.append(
                        {
                            "condition": condition,
                            "latency_ms": round(elapsed, 3),
                            "tokens": detail["usage"]["output_tokens"],
                            "bytes": len(emitted[0]),
                            "cache": detail["cache"],
                            "selected": len(detail["selected"]),
                            "status": value["status"],
                        }
                    )

            def summary(condition):
                values = sorted(r["latency_ms"] for r in rows if r["condition"] == condition)
                return {
                    "samples": len(values),
                    "p50_ms": statistics.median(values),
                    "p95_ms": values[math.ceil(0.95 * len(values)) - 1],
                    "p99_ms": values[math.ceil(0.99 * len(values)) - 1],
                }

            artifact_bytes = store.db.execute(
                "SELECT sum(length(body)) FROM rendered_artifacts"
            ).fetchone()[0]
    result = {
        "schema_version": 1,
        "phase": 4,
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "corpus": {
            "episodes": 64,
            "matching_episodes": 16,
            "claims": 0,
            "constraints": 0,
            "principals": 1,
            "concurrency": 1,
            "classification": "synthetic",
        },
        "summary": {c: summary(c) for c in dict.fromkeys(r["condition"] for r in rows)},
        "samples": rows,
        "retained_body_bytes": artifact_bytes,
        "budget_compliant": all(r["tokens"] <= 1500 for r in rows),
        "errors": 0,
        "provider_calls": 0,
        "provider_cost": "not_applicable_no_provider",
        "limits": [
            "single small local corpus",
            "no skewed-tenant contention",
            "no quality or savings inference",
            "percentiles are descriptive, not confidence bounds",
            "receipt inspection excluded from request latency",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["summary"]))


if __name__ == "__main__":
    main()
