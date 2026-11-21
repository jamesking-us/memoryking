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
# The embedding models that may be installed and enabled; each has a manifest under models/.
SUPPORTED = (MODEL,)
RERANKER = "ms-marco-minilm-l6-v2-onnx"
RERANK_PAIRS = 16  # query/text pairs the model scores in one batch
RERANK_MAX_PAIRS = 64  # pairs one call may ask for; it runs them in batches of RERANK_PAIRS
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
    """One CPU thread by default, sequential inference, at most 16 texts per call.

    The manifest names the pooling (mean over the attention mask, or the first token), the
    token limit and the prefixes a model wants on a query and on a document, so a fingerprint
    covers all of them. `embed` takes documents; pass `kind="query"` for a search question."""

    kinds = True  # `embed` takes a `kind`: a document or a query

    def __init__(self, directory: Path, name: str = MODEL, threads: int = 1):
        self.configuration = model_manifest(name)
        self.name = name
        self.fingerprint = fingerprint(self.configuration)
        self.dimensions = self.configuration["dimensions"]
        self.limit = self.configuration["max_input_tokens"]
        self.pooling = self.configuration["pooling"]
        if self.pooling not in {"attention-mask-mean-v1", "cls-v1"}:
            raise LocalError("embedding_model_invalid")
        self.prefixes = {
            "document": self.configuration.get("document_prefix", ""),
            "query": self.configuration.get("query_prefix", ""),
        }
        data = model_files(directory, self.configuration)
        ort, tokenizers, self.np = _runtime(self.configuration)
        self.lock = threading.Lock()
        try:
            self.session = _session(
                ort, data[self.configuration.get("model_file", "onnx/model.onnx")], threads
            )
            self.inputs = {item.name for item in self.session.get_inputs()}
            outputs = [item.name for item in self.session.get_outputs()]
            self.output = "last_hidden_state" if "last_hidden_state" in outputs else outputs[0]
            self.tokenizer = tokenizers.Tokenizer.from_str(data["tokenizer.json"].decode())
            pad = self.configuration.get("pad_token", "[PAD]")
            pad_id = self.tokenizer.token_to_id(pad)
            self.tokenizer.enable_padding(pad_id=0 if pad_id is None else pad_id, pad_token=pad)
        except Exception as exc:
            raise LocalError("embedding_model_invalid") from exc

    def embed(self, texts: list[str], kind: str = "document") -> EmbeddingBatch:
        validate_inputs(texts)
        prefix = self.prefixes[kind]
        prefixed = [prefix + text for text in texts] if prefix else texts
        with self.lock:
            try:
                self.tokenizer.no_truncation()
                full = self.tokenizer.encode_batch(prefixed)
                lengths = [sum(item.attention_mask) for item in full]
                self.tokenizer.enable_truncation(max_length=self.limit)
                tokens = self.tokenizer.encode_batch(prefixed)
                values = {
                    "input_ids": [item.ids for item in tokens],
                    "attention_mask": [item.attention_mask for item in tokens],
                    "token_type_ids": [item.type_ids for item in tokens],
                }
                inputs = {
                    name: self.np.asarray(value, dtype=self.np.int64)
                    for name, value in values.items()
                    if name in self.inputs
                }
                output = self.session.run([self.output], inputs)[0]
                if self.pooling == "cls-v1":
                    pooled = output[:, 0, :]
                else:
                    mask = inputs["attention_mask"][..., None]
                    pooled = (output * mask).sum(axis=1) / mask.sum(axis=1).clip(min=1)
                batch = EmbeddingBatch(
                    self.fingerprint,
                    self.dimensions,
                    [quantize(vector.tolist(), self.dimensions) for vector in pooled],
                    [canonical_hash(text) for text in texts],
                    sum(sum(item.attention_mask) for item in tokens),
                    [length > self.limit for length in lengths],
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

    def __init__(self, directory: Path, name: str = RERANKER, threads: int | None = None):
        self.configuration = model_manifest(name)
        self.name = name
        self.fingerprint = fingerprint(self.configuration)
        self.limit = self.configuration.get("max_input_tokens", RERANK_TOKENS)
        data = model_files(directory, self.configuration)
        ort, tokenizers, self.np = _runtime(self.configuration)
        self.lock = threading.Lock()
        try:
            self.session = _session(
                ort,
                data[self.configuration.get("model_file", "onnx/model.onnx")],
                threads or min(RERANK_THREADS, os.cpu_count() or 1),
            )
            self.inputs = {item.name for item in self.session.get_inputs()}
            self.tokenizer = tokenizers.Tokenizer.from_str(data["tokenizer.json"].decode())
            pad = self.configuration.get("pad_token", "[PAD]")
            pad_id = self.tokenizer.token_to_id(pad)
            self.tokenizer.enable_padding(pad_id=0 if pad_id is None else pad_id, pad_token=pad)
            self.tokenizer.enable_truncation(max_length=self.limit, strategy="only_second")
        except Exception as exc:
            raise LocalError("reranker_model_invalid") from exc

    def score(self, query: str, texts: list[str]) -> list[int]:
        """Relevance of each text to the query, in millionths, in the texts' order."""
        if (
            not isinstance(query, str)
            or not query
            or len(query.encode()) > RERANK_TEXT_BYTES
            or not isinstance(texts, list)
            or not 1 <= len(texts) <= RERANK_MAX_PAIRS
            or any(
                not isinstance(text, str) or not text or len(text.encode()) > RERANK_TEXT_BYTES
                for text in texts
            )
        ):
            raise LocalError("reranker_input_limit")
        scores: list[int] = []
        with self.lock:
            try:
                for start in range(0, len(texts), RERANK_PAIRS):
                    group = texts[start : start + RERANK_PAIRS]
                    encoded = self.tokenizer.encode_batch([(query, text) for text in group])
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
                    scores += [
                        int(round(1_000_000 / (1 + math.exp(-float(row[0] if len(row) else row)))))
                        for row in logits.reshape(len(group), -1)
                    ]
                return scores
            except LocalError:
                raise
            except Exception as exc:
                raise LocalError("reranker_failure") from exc
