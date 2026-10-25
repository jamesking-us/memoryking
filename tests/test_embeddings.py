"""Core vector integrity and hash-pinned model loading, without model downloads."""

import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memoryking.adapters.local_embeddings import LocalOnnxEmbedding, model_files, model_manifest
from memoryking.contracts.canonical import canonical_hash
from memoryking.domain.embeddings import (
    EmbeddingBatch,
    cosine_ppm,
    fingerprint,
    quantize,
    validate_inputs,
    validate_vector,
)
from memoryking.domain.local import LocalError


class EmbeddingTests(unittest.TestCase):
    def test_normalization_and_finite_integer_vector_integrity(self):
        left = quantize([3.0, 4.0], 2)
        self.assertEqual(left, [600000, 800000])
        self.assertEqual(cosine_ppm(left, left), 1000000)
        self.assertEqual(cosine_ppm(left, [-x for x in left]), -1000000)
        self.assertEqual(cosine_ppm([1000000, 0], [0, 1000000]), 0)
        for values in ([0.0, 0.0], [float("nan"), 1], [float("inf"), 0], [True, 1]):
            with self.assertRaises(LocalError):
                quantize(values, 2)
        for vector, dimensions in (
            ([1], 2),
            ([0, 0], 2),
            ([True, 0], 2),
            ([1000001, 0], 2),
            ([1], True),
        ):
            with self.assertRaises(LocalError):
                validate_vector(vector, dimensions)

    def test_results_are_bound_to_fingerprint_dimension_order_and_input_hashes(self):
        inputs = ["alpha", "beta"]
        batch = EmbeddingBatch(
            "a" * 64,
            2,
            [[1000000, 0], [0, 1000000]],
            [canonical_hash(text) for text in inputs],
            4,
            [False, False],
        )
        batch.validate("a" * 64, 2, inputs)
        for config, dims, text in [
            ("b" * 64, 2, inputs),
            ("a" * 64, 3, inputs),
            ("a" * 64, 2, inputs[::-1]),
        ]:
            with self.assertRaises(LocalError):
                batch.validate(config, dims, text)

    def test_all_model_and_processing_changes_partition_fingerprint(self):
        configuration = model_manifest()
        initial = fingerprint(configuration)
        for key, value in [
            ("revision", "b" * 40),
            ("dimensions", 768),
            ("pooling", "changed"),
            ("max_input_tokens", 128),
            ("normalization", "changed"),
        ]:
            changed = copy.deepcopy(configuration)
            changed[key] = value
            self.assertNotEqual(initial, fingerprint(changed))
        changed = copy.deepcopy(configuration)
        changed["files"][0]["sha256"] = "f" * 64
        self.assertNotEqual(initial, fingerprint(changed))

    def test_input_limits_apply_to_bytes_batch_and_aggregate(self):
        validate_inputs(["a", "🔒"])
        for inputs in ([], ["x"] * 17, [""], ["🔒" * 4097], ["x" * 16384] * 9, "abc"):
            with self.assertRaises(LocalError):
                validate_inputs(inputs)

    def test_absent_model_never_fetches_and_changed_bytes_reject(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
                with self.assertRaises(LocalError):
                    LocalOnnxEmbedding(root)
            data = b"known fixture model bytes"
            model = root / "test.bin"
            model.write_bytes(data)
            manifest = {
                "files": [
                    {
                        "path": "test.bin",
                        "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                ]
            }
            with patch(
                "memoryking.adapters.local_embeddings.model_manifest", return_value=manifest
            ):
                self.assertEqual(model_files(root)["test.bin"], data)
                model.write_bytes(b"x" * len(data))
                with self.assertRaisesRegex(LocalError, "checksum_mismatch"):
                    model_files(root)
