"""Exercise the pinned real local model on synthetic development text only."""

import argparse
import dataclasses
import json
import platform
import time
from pathlib import Path

from memoryking.adapters.local_embeddings import LocalOnnxEmbedding
from memoryking.domain.embeddings import cosine_ppm


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Choose a new evidence output path")
    texts = [
        "The database query timed out.",
        "A database request exceeded its time limit.",
        "A violinist played a concerto.",
        "word " * 600,
    ]
    started = time.monotonic()
    provider = LocalOnnxEmbedding(args.model)
    loaded = time.monotonic()
    batch = provider.embed(texts)
    elapsed = time.monotonic() - loaded
    batch.validate(provider.fingerprint, 384, texts)
    duplicate = provider.embed(texts[:1])
    positive = cosine_ppm(batch.vectors[0], batch.vectors[1])
    unrelated = cosine_ppm(batch.vectors[0], batch.vectors[2])
    assert positive > unrelated
    assert batch.truncated == [False, False, False, True]
    assert batch.input_tokens is not None and batch.input_tokens > 256
    assert duplicate.vectors[0] == batch.vectors[0]
    result = {
        "scope": "synthetic local inference smoke test; not task quality qualification",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "configuration": provider.configuration,
        "fingerprint": provider.fingerprint,
        "load_ms": round((loaded - started) * 1000),
        "inference_ms": round(elapsed * 1000),
        "pair_similarity_ppm": positive,
        "unrelated_similarity_ppm": unrelated,
        "batch": dataclasses.asdict(batch),
        "network_calls": 0,
        "passed": True,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: result[key]
                for key in [
                    "passed",
                    "fingerprint",
                    "load_ms",
                    "inference_ms",
                    "pair_similarity_ppm",
                    "unrelated_similarity_ppm",
                ]
            }
        )
    )


if __name__ == "__main__":
    main()
