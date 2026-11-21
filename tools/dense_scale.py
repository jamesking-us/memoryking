"""Latency of the authorized dense scan at 10,000, 100,000 and 1,000,000 vectors.

Synthetic: one real capture is copied into N observation rows and N random unit vectors are
written straight into `embedding_vectors`, as 16-bit integers of 384 dimensions, so this
measures the storage query (eligibility, audience, newest first) and the matrix product, not
embedding throughput or model quality. The store is local SQLite in a short temporary
directory; each size is timed five times and the median and the worst are reported, once
with the bound lifted (the cost of the whole corpus) and once under the product's bound.

    python tools/dense_scale.py --sizes 10000,100000,1000000 --output /tmp/mk-dense/scale.json
"""

from __future__ import annotations

import argparse
import json
import platform
import resource
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

from memoryking.adapters import dense_search  # noqa: E402
from memoryking.adapters.local_store import LocalStore  # noqa: E402
from memoryking.contracts.canonical import canonical_hash  # noqa: E402

DIMENSIONS = 384
QUERY = "a question about latency"


def build(store: LocalStore, credential: str, size: int) -> tuple[dict[str, Any], str]:
    tenant = store._decode(credential, "credential")["tenant"]
    captured = store.capture(
        credential,
        store.namespace(credential, "scale"),
        "initial",
        {
            "version": 1,
            "project": "scale",
            "source": "synthetic",
            "source_event": "initial",
            "text": "A synthetic observation for scale measurement.",
            "readers": None,
            "classification": "internal",
            "observed_at_ms": 1,
        },
    )
    with store.transaction() as decision:
        original = dict(
            store.db.execute(
                "SELECT * FROM observations WHERE tenant=? AND id=?",
                (tenant, captured["observation"]),
            ).fetchone()
        )
        fingerprint = "f" * 64
        store.db.execute(
            "INSERT INTO embedding_partitions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                tenant,
                "scale",
                fingerprint,
                store.incarnation,
                1,
                DIMENSIONS,
                "{}",
                "experimental",
                0,
                0,
                decision.evaluated_ms,
            ),
        )
        rng = np.random.default_rng(7)
        names = list(original)
        for start in range(0, size, 50000):
            count = min(50000, size - start)
            raw = rng.standard_normal((count, DIMENSIONS)).astype(np.float32)
            raw /= np.linalg.norm(raw, axis=1, keepdims=True)
            packed = np.rint(raw * 32767).astype("<i2")
            observations, vectors = [], []
            for offset in range(count):
                number = start + offset
                row = dict(original, id=f"obs_scale_{number:08}", source_event=f"s{number}")
                observations.append(tuple(row[name] for name in names))
                vectors.append(
                    (
                        tenant,
                        "scale",
                        fingerprint,
                        1,
                        "observation",
                        row["id"],
                        0,
                        store.incarnation,
                        row["revision"],
                        row["blob"],
                        "0" * 64,
                        packed[offset].tobytes(),
                        0,
                        50,
                        None,
                        0,
                        1000 + number,  # the vector's own order, newest last
                    )
                )
            store.db.executemany(
                "INSERT INTO observations VALUES (" + ",".join("?" for _ in names) + ")",
                observations,
            )
            store.db.executemany(
                "INSERT INTO embedding_vectors VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", vectors
            )
        store.db.execute(
            "UPDATE embedding_partitions SET scheduled=?,frontier=? WHERE tenant=?",
            (10**9, 10**9, tenant),
        )
    return {"tenant": tenant, "principal": "owner"}, fingerprint


def measure(
    store: LocalStore, who: dict[str, Any], fingerprint: str, eligible: int, lifted: bool
) -> dict[str, Any]:
    rng = np.random.default_rng(11)
    vector = (rng.standard_normal(DIMENSIONS) / 1).astype(np.float32)
    vector = np.rint(vector / np.linalg.norm(vector) * 1_000_000).astype(int).tolist()
    args = {
        "project": "scale",
        "query": QUERY,
        "dense": {
            "fingerprint": fingerprint,
            "query_hash": canonical_hash(QUERY),
            "vector": vector,
            "minimum_similarity_ppm": 0,
        },
    }
    original = dense_search.SCAN_VECTORS
    if lifted:
        dense_search.SCAN_VECTORS = 10**9
    try:
        times, result = [], {}
        for _ in range(5):
            started = time.perf_counter()
            result = dense_search.search(store, who, args, 10**9, 10**9)
            times.append((time.perf_counter() - started) * 1000)
    finally:
        dense_search.SCAN_VECTORS = original
    return {
        "eligible": eligible,
        "bound_lifted": lifted,
        "median_ms": round(statistics.median(times), 1),
        "worst_ms": round(max(times), 1),
        "availability": result["availability"],
        "eligible_vectors": result.get("eligible_vectors"),
        "scan_truncated": result.get("scan_truncated", False),
        "scores": len(result["scores"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="10000,100000,1000000")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    sizes = [int(s) for s in args.sizes.split(",")]
    rows: list[dict[str, Any]] = []
    built: dict[int, float] = {}
    for size in sizes:
        with tempfile.TemporaryDirectory(prefix="mk-dense-", dir="/tmp") as temp:
            data, control = Path(temp) / "d", Path(temp) / "c"
            credential = LocalStore.initialize(data, control).read_text()
            store = LocalStore(data, control)
            try:
                started = time.perf_counter()
                who, fingerprint = build(store, credential, size)
                built[size] = round(time.perf_counter() - started, 1)
                rows.append(measure(store, who, fingerprint, size, True))
                rows.append(measure(store, who, fingerprint, size, False))
            finally:
                store.close()
    report = {
        "machine": platform.processor() or platform.machine(),
        "python": platform.python_version(),
        "numpy": np.__version__,
        "dimensions": DIMENSIONS,
        "bytes_per_vector": DIMENSIONS * 2,
        "scan_vectors": dense_search.SCAN_VECTORS,
        "build_seconds": built,
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024),
        "rows": rows,
    }
    text = json.dumps(report, indent=2)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")


if __name__ == "__main__":
    main()
