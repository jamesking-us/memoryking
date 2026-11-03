"""Explicitly installed, hash-pinned ONNX CPU inference; no downloads or remote code."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import math
import os
import threading
from pathlib import Path
from typing import Any

from memoryking.contracts.canonical import canonical_hash
from memoryking.domain.embeddings import EmbeddingBatch, fingerprint, quantize, validate_inputs
from memoryking.domain.local import LocalError

MODEL = "minilm-l6-v2-onnx"
RERANKER = "ms-marco-minilm-l6-v2-onnx"
RERANK_PAIRS = 16  # query/text pairs scored per call
RERANK_TEXT_BYTES = 16384
RERANK_TOKENS = 256  # per pair; the text is cut, never the query
RERANK_THREADS = 4  # the reranker may use up to this many CPU threads


def model_manifest(name: str = MODEL) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(
        Path(__file__).with_name("models").joinpath(name + ".json").read_text()
    )
    return value


def model_files(directory: Path, manifest: dict[str, Any] | None = None) -> dict[str, bytes]:
    manifest = manifest or model_manifest()
    prefix = (
        "reranker_model" if manifest.get("schema") == "reranker-model-v1" else "embedding_model"
    )
    result = {}
    for file in manifest["files"]:
        path = directory / file["path"]
        if path.is_symlink() or not path.is_file() or path.stat().st_size != file["bytes"]:
            raise LocalError(prefix + "_not_installed")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != file["sha256"]:
            raise LocalError(prefix + "_checksum_mismatch")
        result[file["path"]] = raw
    return result


def _runtime(configuration: dict[str, Any]) -> tuple[Any, Any, Any]:
    """The pinned ONNX runtime, tokenizers and numpy, or the one error that names them."""
    try:
        for name, version in configuration["runtime"].items():
            if importlib.metadata.version(name) != version:
                raise LocalError("embedding_runtime_mismatch")
        ort = importlib.import_module("onnxruntime")
        tokenizers = importlib.import_module("tokenizers")
        np = importlib.import_module("numpy")
    except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
        raise LocalError("embedding_runtime_unavailable") from exc
    return ort, tokenizers, np


def _session(ort: Any, data: bytes, threads: int = 1) -> Any:
    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.log_severity_level = 3
    # Load the exact checked bytes, not a path that can change after hashing.
    return ort.InferenceSession(data, sess_options=options, providers=["CPUExecutionProvider"])


class LocalOnnxEmbedding:
    """One CPU thread, sequential inference, max 16 texts and 256 tokens each."""

    def __init__(self, directory: Path):
        self.configuration = model_manifest()
        self.fingerprint = fingerprint(self.configuration)
        self.dimensions = self.configuration["dimensions"]
        data = model_files(directory)
        ort, tokenizers, self.np = _runtime(self.configuration)
        self.lock = threading.Lock()
        try:
            self.session = _session(ort, data["onnx/model.onnx"])
            self.tokenizer = tokenizers.Tokenizer.from_str(data["tokenizer.json"].decode())
            self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        except Exception as exc:
            raise LocalError("embedding_model_invalid") from exc

    def embed(self, texts: list[str]) -> EmbeddingBatch:
        validate_inputs(texts)
        with self.lock:
            try:
                self.tokenizer.no_truncation()
                full = self.tokenizer.encode_batch(texts)
                lengths = [sum(item.attention_mask) for item in full]
                self.tokenizer.enable_truncation(max_length=256)
                tokens = self.tokenizer.encode_batch(texts)
                values = {
                    "input_ids": [item.ids for item in tokens],
                    "attention_mask": [item.attention_mask for item in tokens],
                    "token_type_ids": [item.type_ids for item in tokens],
                }
                inputs = {
                    name: self.np.asarray(value, dtype=self.np.int64)
                    for name, value in values.items()
                }
                output = self.session.run(None, inputs)[0]
                mask = inputs["attention_mask"][..., None]
                pooled = (output * mask).sum(axis=1) / mask.sum(axis=1).clip(min=1)
                batch = EmbeddingBatch(
                    self.fingerprint,
                    self.dimensions,
                    [quantize(vector.tolist(), self.dimensions) for vector in pooled],
                    [canonical_hash(text) for text in texts],
                    sum(sum(item.attention_mask) for item in tokens),
                    [length > 256 for length in lengths],
                )
                batch.validate(self.fingerprint, self.dimensions, texts)
                return batch
            except LocalError:
                raise
            except Exception as exc:
                raise LocalError("embedding_provider_failure") from exc


class LocalOnnxReranker:
    """A cross-encoder that scores a query against each candidate text on the CPU.

    At most 16 pairs per call and 256 tokens per pair (the text is what gets cut, never
    the query), on up to four threads, so a call stays well inside the five-second
    release deadline of a request. The score is the model's relevance logit through a
    sigmoid, rounded to millionths, so it is comparable across calls and replayable."""

    def __init__(self, directory: Path):
        self.configuration = model_manifest(RERANKER)
        self.fingerprint = fingerprint(self.configuration)
        data = model_files(directory, self.configuration)
        ort, tokenizers, self.np = _runtime(self.configuration)
        self.lock = threading.Lock()
        try:
            self.session = _session(
                ort, data["onnx/model.onnx"], min(RERANK_THREADS, os.cpu_count() or 1)
            )
            self.inputs = {item.name for item in self.session.get_inputs()}
            self.tokenizer = tokenizers.Tokenizer.from_str(data["tokenizer.json"].decode())
            self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
            self.tokenizer.enable_truncation(max_length=RERANK_TOKENS, strategy="only_second")
        except Exception as exc:
            raise LocalError("reranker_model_invalid") from exc

    def score(self, query: str, texts: list[str]) -> list[int]:
        """Relevance of each text to the query, in millionths, in the texts' order."""
        if (
            not isinstance(query, str)
            or not query
            or len(query.encode()) > RERANK_TEXT_BYTES
            or not isinstance(texts, list)
            or not 1 <= len(texts) <= RERANK_PAIRS
            or any(
                not isinstance(text, str) or not text or len(text.encode()) > RERANK_TEXT_BYTES
                for text in texts
            )
        ):
            raise LocalError("reranker_input_limit")
        with self.lock:
            try:
                encoded = self.tokenizer.encode_batch([(query, text) for text in texts])
                values = {
                    "input_ids": [item.ids for item in encoded],
                    "attention_mask": [item.attention_mask for item in encoded],
                    "token_type_ids": [item.type_ids for item in encoded],
                }
                inputs = {
                    name: self.np.asarray(value, dtype=self.np.int64)
                    for name, value in values.items()
                    if name in self.inputs
                }
                logits = self.session.run(None, inputs)[0]
                return [
                    int(round(1_000_000 / (1 + math.exp(-float(row[0] if len(row) else row)))))
                    for row in logits.reshape(len(texts), -1)
                ]
            except LocalError:
                raise
            except Exception as exc:
                raise LocalError("reranker_failure") from exc
