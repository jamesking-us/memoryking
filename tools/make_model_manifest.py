#!/usr/bin/env python3
"""Pin a Hugging Face model for a manifest: download one revision, hash it, write the JSON.

A maintainer's tool, not part of an install: it needs the network and writes the manifest
that `memoryking.model_install` later verifies against. The weights are fetched into
`--directory` (which then also works as an installed model directory) and are never copied
into the repository; only the manifest, with the revision, sizes and SHA-256 hashes, is.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path
from typing import Any

RUNTIME = {"onnxruntime": "1.23.2", "numpy": "2.5.4", "tokenizers": "0.23.3"}


def api(path: str) -> dict[str, Any]:
    with urllib.request.urlopen("https://huggingface.co/api/" + path, timeout=60) as response:
        value: dict[str, Any] = json.load(response)
    return value


def fetch(repository: str, revision: str, name: str, target: Path) -> dict[str, Any]:
    url = f"https://huggingface.co/{repository}/resolve/{revision}/{name}"
    path = target / name
    path.parent.mkdir(parents=True, exist_ok=True)
    digest, size = hashlib.sha256(), 0
    with urllib.request.urlopen(url, timeout=120) as response, path.open("wb") as out:
        while chunk := response.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
            out.write(chunk)
    return {"path": name, "url": url, "bytes": size, "sha256": digest.hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="owner/name on huggingface.co")
    parser.add_argument("--id", required=True, help="manifest id, e.g. bge-small-en-v1-5-onnx")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--revision", help="commit to pin; default the repository's current head")
    parser.add_argument("--kind", choices=["embedding", "reranker"], default="embedding")
    parser.add_argument("--model-file", default="onnx/model.onnx")
    parser.add_argument("--file", action="append", default=[], help="another file to pin")
    parser.add_argument("--pooling", default="attention-mask-mean-v1")
    parser.add_argument("--max-input-tokens", type=int, default=512)
    parser.add_argument("--dimensions", type=int)
    parser.add_argument("--query-prefix", default="")
    parser.add_argument("--document-prefix", default="")
    parser.add_argument("--pad-token", default="[PAD]")
    parser.add_argument("--output", type=Path, help="write the manifest here")
    args = parser.parse_args()
    info = api("models/" + args.repository)
    revision = args.revision or info["sha"]
    licence = next(
        (t.split(":", 1)[1] for t in info.get("tags", []) if t.startswith("license:")), None
    )
    names = ["tokenizer.json", args.model_file, *args.file]
    siblings = {s["rfilename"] for s in info["siblings"]}
    if args.model_file + "_data" in siblings:
        names.append(args.model_file + "_data")
    files = [fetch(args.repository, revision, name, args.directory) for name in names]
    manifest: dict[str, Any] = {
        "schema": "reranker-model-v1" if args.kind == "reranker" else "embedding-model-v1",
        "id": args.id,
        "provider": "local-onnx",
        "repository": args.repository,
        "revision": revision,
        "license": licence,
    }
    if args.kind == "embedding":
        manifest.update(
            dimensions=args.dimensions,
            max_input_tokens=args.max_input_tokens,
            pooling=args.pooling,
            normalization="l2-then-round-1e6-v1",
        )
        if args.query_prefix:
            manifest["query_prefix"] = args.query_prefix
        if args.document_prefix:
            manifest["document_prefix"] = args.document_prefix
        if args.pad_token != "[PAD]":
            manifest["pad_token"] = args.pad_token
    else:
        manifest.update(
            kind="cross-encoder",
            max_input_tokens=args.max_input_tokens,
            truncation="only-second-v1",
            scoring="sigmoid-logit-round-1e6-v1",
        )
        if args.pad_token != "[PAD]":
            manifest["pad_token"] = args.pad_token
    if args.model_file != "onnx/model.onnx":
        manifest["model_file"] = args.model_file
    manifest.update(
        files=files, runtime=RUNTIME, activation="experimental-off-by-default", remote_code=False
    )
    text = json.dumps(manifest, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
