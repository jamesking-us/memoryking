"""Worker control/dispatch tests use fixture vectors, never model-quality claims."""

import time
import unittest
from unittest.mock import Mock, patch

import test_sdk as sdk

from memoryking.adapters.local_embeddings import LocalOnnxEmbedding, model_manifest
from memoryking.domain.embeddings import EmbeddingBatch, fingerprint
from memoryking.domain.local import LocalError
from memoryking.embedding_worker import run_once


class EmbeddingWorkerTests(unittest.TestCase):
    setUp = sdk.SDKTests.setUp
    tearDown = sdk.SDKTests.tearDown

    def provider(self):
        provider = object.__new__(LocalOnnxEmbedding)
        provider.fingerprint = fingerprint(model_manifest())
        provider.dimensions = 384
        self.client.embedding(
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": "configure",
                "key": "worker-config",
                "args": {
                    "project": "p",
                    "model": "minilm-l6-v2-onnx",
                    "state": "experimental",
                    "expected_generation": 0,
                },
            }
        )
        return provider

    def test_expired_dispatch_does_not_send_and_records_known_zero(self):
        provider = self.provider()
        now = time.time()
        with (
            patch.object(provider, "embed") as embed,
            patch("memoryking.embedding_worker.time", side_effect=[now, now + 6]),
            patch("memoryking.embedding_worker.monotonic", side_effect=[0, 6, 6]),
        ):
            result = run_once(self.client, provider)
        self.assertEqual(result["error"], "embedding_dispatch_expired")
        embed.assert_not_called()
        usage = self.store.db.execute("SELECT status,input_tokens FROM processing_usage").fetchone()
        self.assertEqual(tuple(usage), ("failed", 0))

    def test_provider_failure_is_one_attempt_and_usage_unknown(self):
        provider = self.provider()
        with patch.object(provider, "embed", side_effect=RuntimeError("secret")) as embed:
            result = run_once(self.client, provider)
        self.assertEqual(result["status"], "attempt_failed")
        self.assertNotIn("secret", str(result))
        embed.assert_called_once()
        usage = self.store.db.execute("SELECT status,input_tokens FROM processing_usage").fetchone()
        self.assertEqual(tuple(usage), ("failed", None))

    def test_success_actual_socket_and_uncertain_completion_never_retries(self):
        from memoryking.contracts.canonical import canonical_hash

        provider = self.provider()

        def embed(texts):
            return EmbeddingBatch(
                provider.fingerprint,
                384,
                [[1000000] + [0] * 383],
                [canonical_hash(texts[0])],
                17,
                [False],
            )

        with patch.object(provider, "embed", side_effect=embed) as dispatch:
            result = run_once(self.client, provider)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(run_once(self.client, provider)["status"], "idle")
            dispatch.assert_called_once()
        usage = self.store.db.execute("SELECT status,input_tokens FROM processing_usage").fetchone()
        self.assertEqual(tuple(usage), ("succeeded", 17))

        # Lose the completion response after server commit; keep the completed
        # job and usage intact without resending inference or a failure mutation.
        self.client.capture(
            "next",
            {
                "version": 1,
                "project": "p",
                "source": "test",
                "source_event": "next",
                "text": "another observation",
                "readers": None,
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )
        original = self.client.embedding

        def transport(command):
            value = original(command)
            if command["action"] == "complete":
                raise LocalError("transport_unavailable")
            return value

        with (
            patch.object(provider, "embed", side_effect=embed) as dispatch,
            patch.object(self.client, "embedding", side_effect=transport) as calls,
        ):
            result = run_once(self.client, provider)
            self.assertEqual(result["status"], "reconciliation_required")
            dispatch.assert_called_once()
            self.assertEqual(
                [c.args[0]["action"] for c in calls.call_args_list], ["claim", "complete"]
            )

    def test_arbitrary_provider_rejected_before_claim(self):
        client = Mock()
        with self.assertRaisesRegex(LocalError, "embedding_provider_unsupported"):
            run_once(client, Mock())
        client.embedding.assert_not_called()
