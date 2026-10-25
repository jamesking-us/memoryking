"""Explicitly installed, hash-pinned ONNX CPU inference; no downloads or remote code."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import threading
from pathlib import Path
from typing import Any

from memoryking.contracts.canonical import canonical_hash
from memoryking.domain.embeddings import EmbeddingBatch, fingerprint, quantize, validate_inputs
from memoryking.domain.local import LocalError

MODEL = "minilm-l6-v2-onnx"


def model_manifest() -> dict[str, Any]:
    value: dict[str, Any] = json.loads(
        Path(__file__).with_name("models").joinpath(MODEL + ".json").read_text()
    )
    return value


def model_files(directory: Path) -> dict[str, bytes]:
    result = {}
    for file in model_manifest()["files"]:
        path = directory / file["path"]
        if path.is_symlink() or not path.is_file() or path.stat().st_size != file["bytes"]:
            raise LocalError("embedding_model_not_installed")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != file["sha256"]:
            raise LocalError("embedding_model_checksum_mismatch")
        result[file["path"]] = raw
    return result


class LocalOnnxEmbedding:
    """One CPU thread, sequential inference, max 16 texts and 256 tokens each."""

    def __init__(self, directory: Path):
        self.configuration = model_manifest()
        self.fingerprint = fingerprint(self.configuration)
        self.dimensions = self.configuration["dimensions"]
        data = model_files(directory)
        try:
            for name, version in self.configuration["runtime"].items():
                if importlib.metadata.version(name) != version:
                    raise LocalError("embedding_runtime_mismatch")
            ort = importlib.import_module("onnxruntime")
            tokenizers = importlib.import_module("tokenizers")
            self.np = importlib.import_module("numpy")
        except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
            raise LocalError("embedding_runtime_unavailable") from exc
        self.lock = threading.Lock()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.log_severity_level = 3
        try:
            # Load the exact checked bytes, not a path that can change after hashing.
            self.session = ort.InferenceSession(
                data["onnx/model.onnx"], sess_options=options, providers=["CPUExecutionProvider"]
            )
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
