"""Real local model, daemon, SDK and durable job/usage smoke on synthetic text."""

import argparse
import json
import platform
import tempfile
import threading
import time
from pathlib import Path

from memoryking.adapters.local_embeddings import LocalOnnxEmbedding
from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
from memoryking.contracts.canonical import canonical_hash
from memoryking.embedding_worker import run_once
from memoryking.sdk import LocalClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new evidence path")
    provider = LocalOnnxEmbedding(args.model)
    with tempfile.TemporaryDirectory(prefix="mk-ew-", dir="/tmp") as temp:
        root = Path(temp)
        credential = LocalStore.initialize(root / "data", root / "control")
        with LocalStore(root / "data", root / "control") as store:
            server = Server(store)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                client = LocalClient(server.path, credential, "demo")
                for n, text in enumerate(
                    [
                        "The database query timed out.",
                        "A database request exceeded its time limit.",
                        "A violinist played a concerto.",
                    ]
                ):
                    client.capture(
                        str(n),
                        {
                            "version": 1,
                            "project": "demo",
                            "source": "synthetic-development",
                            "source_event": str(n),
                            "text": text,
                            "readers": None,
                            "classification": "internal",
                            "observed_at_ms": 1,
                        },
                    )
                config = client.embedding(
                    {
                        "version": 1,
                        "incarnation": client.binding[-1],
                        "action": "configure",
                        "key": "config",
                        "args": {
                            "project": "demo",
                            "model": "minilm-l6-v2-onnx",
                            "state": "experimental",
                            "expected_generation": 0,
                        },
                    }
                )
                start = time.monotonic()
                results = [run_once(client, provider) for _ in range(4)]
                elapsed = round((time.monotonic() - start) * 1000)
                assert [r["status"] for r in results] == ["complete"] * 3 + ["idle"]
                assert results[-1]["ready"]
                usage = [
                    dict(r)
                    for r in store.db.execute(
                        "SELECT stage,status,input_tokens,cost_microusd,duration_ms FROM processing_usage"
                    ).fetchall()
                ]
                assert len(usage) == 3 and all(r["input_tokens"] > 0 for r in usage)
                assert store.db.execute("SELECT count(*) FROM embedding_items").fetchone()[0] == 3
                query = "SQL latency"
                query_batch = provider.embed([query])
                brief_args = {
                    "project": "demo",
                    "query": query,
                    "bindings": [{}],
                    "tokenizer": "unknown-model",
                    "max_output_tokens": 10000,
                }
                lexical = client.retrieval(
                    {
                        "version": 1,
                        "incarnation": client.binding[-1],
                        "action": "brief",
                        "args": brief_args,
                    }
                )
                semantic = client.retrieval(
                    {
                        "version": 1,
                        "incarnation": client.binding[-1],
                        "action": "brief",
                        "args": {
                            **brief_args,
                            "dense": {
                                "fingerprint": provider.fingerprint,
                                "query_hash": canonical_hash(query),
                                "vector": query_batch.vectors[0],
                            },
                        },
                    }
                )
                assert not lexical["guidance"]
                assert semantic["guidance"] and "database" in semantic["guidance"][0]["text"]
                result = {
                    "passed": True,
                    "scope": "synthetic end-to-end smoke, not task quality",
                    "platform": platform.platform(),
                    "fingerprint": config["fingerprint"],
                    "elapsed_ms": elapsed,
                    "usage": usage,
                    "network_model_calls": 0,
                    "frontier": results[-1]["frontier"],
                    "ready": True,
                    "query_embedding_tokens": query_batch.input_tokens,
                    "lexical_results": len(lexical["guidance"]),
                    "semantic_results": [
                        {"text": item["text"], "cosine_ppm": item["dense_score_ppm"]}
                        for item in semantic["guidance"]
                    ],
                }
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(result, indent=2) + "\n")
                print(json.dumps(result))
            finally:
                server.shutdown()
                thread.join()
                server.server_close()


if __name__ == "__main__":
    main()
