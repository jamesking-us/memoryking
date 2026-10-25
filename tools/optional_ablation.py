"""Frozen synthetic development comparison of actual lexical and fused retrieval."""

import argparse
import json
import platform
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from memoryking.adapters.local_embeddings import LocalOnnxEmbedding
from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
from memoryking.contracts.canonical import canonical_hash
from memoryking.domain.embeddings import cosine_ppm
from memoryking.domain.usage import UsageRecord, summarize
from memoryking.embedding_worker import run_once
from memoryking.sdk import LocalClient

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a fresh evidence path")
    manifest = json.loads((ROOT / "fixtures/evaluation/p08-development.json").read_text())
    provider = LocalOnnxEmbedding(args.model)
    with tempfile.TemporaryDirectory(prefix="mk-ablation-", dir="/tmp") as tmp:
        root = Path(tmp)
        credential = LocalStore.initialize(root / "data", root / "control")
        with LocalStore(root / "data", root / "control") as store:
            server = Server(store)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                client = LocalClient(server.path, credential, "development")

                def command(action, args):
                    return {
                        "version": 1,
                        "incarnation": client.binding[-1],
                        "action": action,
                        "args": args,
                    }

                ids = {}
                for entry in manifest["observations"]:
                    obs = client.capture(
                        entry["id"],
                        {
                            "version": 1,
                            "project": client.project,
                            "source": "synthetic-development",
                            "source_event": entry["id"],
                            "text": entry["text"],
                            "readers": None,
                            "classification": "internal",
                            "observed_at_ms": 1,
                        },
                    )
                    ids[obs["observation"]] = entry["id"]
                config = command(
                    "configure",
                    {
                        "project": client.project,
                        "model": "minilm-l6-v2-onnx",
                        "expected_generation": 0,
                        "state": "experimental",
                    },
                )
                config["key"] = "configuration"
                client.embedding(config)
                started = time.monotonic()
                backfill = [run_once(client, provider) for _ in range(len(ids) + 1)]
                backfill_ms = round((time.monotonic() - started) * 1000)
                assert all(item["status"] == "complete" for item in backfill[:-1])
                assert backfill[-1]["ready"]
                vectors = {
                    ids[row["observation"]]: json.loads(row["vector_json"])
                    for row in store.db.execute(
                        "SELECT observation,vector_json FROM embedding_items"
                    )
                }
                records = [
                    UsageRecord(
                        row["id"],
                        row["provider"],
                        row["fingerprint"],
                        row["stage"],
                        row["status"],
                        row["input_tokens"],
                        row["cached_input_tokens"],
                        row["output_tokens"],
                        row["cost_microusd"],
                        row["duration_ms"],
                    )
                    for row in store.db.execute("SELECT * FROM processing_usage")
                ]
                results = []
                for query in manifest["queries"]:
                    start = time.monotonic()
                    batch = provider.embed([query["query"]])
                    query_ms = round((time.monotonic() - start) * 1000)
                    records.append(
                        UsageRecord(
                            query["id"],
                            "local-onnx",
                            provider.fingerprint,
                            "query_embedding",
                            "succeeded",
                            batch.input_tokens,
                            0,
                            0,
                            0,
                            query_ms,
                        )
                    )
                    common = {
                        "project": client.project,
                        "query": query["query"],
                        "bindings": [{}],
                        "tokenizer": "cl100k_base",
                        "max_output_tokens": 6000,
                    }
                    arms = {}
                    for arm in ("lexical", "fused"):
                        request = dict(common)
                        if arm == "fused":
                            request["dense"] = {
                                "fingerprint": provider.fingerprint,
                                "query_hash": canonical_hash(query["query"]),
                                "vector": batch.vectors[0],
                            }
                        start = time.monotonic()
                        packet = client.retrieval(command("brief", request))
                        arms[arm] = {
                            "ranked": [ids[item["reference"]] for item in packet["guidance"]][:3],
                            "latency_ms": round((time.monotonic() - start) * 1000),
                            "serialized_tokens": packet["usage"]["output_tokens"],
                            "status": packet["status"],
                        }
                    # Pure channel geometry is a comparison baseline, not a third service endpoint.
                    ranked = sorted(
                        (
                            (cosine_ppm(batch.vectors[0], vector), key)
                            for key, vector in vectors.items()
                        ),
                        reverse=True,
                    )
                    arms["dense_geometry"] = {
                        "ranked": [key for score, key in ranked if score >= 200000][:3]
                    }
                    relevant = set(query["relevant"])
                    for metrics in arms.values():
                        found = len(relevant.intersection(metrics["ranked"]))
                        metrics.update(
                            recall_at_3=None if not relevant else found / len(relevant),
                            precision_at_3=found / 3,
                            empty_truth_abstained=None if relevant else not metrics["ranked"],
                        )
                    results.append({**query, "query_embedding_ms": query_ms, "arms": arms})
                aggregate = {}
                for arm in ("lexical", "fused", "dense_geometry"):
                    cells = [row["arms"][arm] for row in results]
                    recalls = [
                        cell["recall_at_3"] for cell in cells if cell["recall_at_3"] is not None
                    ]
                    aggregate[arm] = {
                        "mean_recall_at_3": sum(recalls) / len(recalls),
                        "mean_precision_at_3": sum(cell["precision_at_3"] for cell in cells)
                        / len(cells),
                        "empty_truth_abstained": sum(
                            cell["empty_truth_abstained"] is True for cell in cells
                        ),
                    }
                result = {
                    "scope": manifest["status"],
                    "manifest_hash": canonical_hash(manifest),
                    "source_revision": subprocess.check_output(
                        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                    ).strip(),
                    "host": platform.platform(),
                    "fingerprint": provider.fingerprint,
                    "backfill_ms": backfill_ms,
                    "queries": results,
                    "aggregates": aggregate,
                    "usage": summarize(records, successful_tasks=0),
                    "network_model_calls": 0,
                    "full_task_cost": "not_observed_no_task_cohort",
                    "activation": "experimental_only",
                    "limitations": "Tiny hand-authored development set, one serial run, no independent quality or percentile qualification.",
                }
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(result, indent=2) + "\n")
                print(
                    json.dumps(
                        {
                            "aggregates": aggregate,
                            "embedding_tokens": result["usage"]["total_tokens"],
                            "activation": result["activation"],
                        }
                    )
                )
            finally:
                server.shutdown()
                thread.join()
                server.server_close()


if __name__ == "__main__":
    main()
