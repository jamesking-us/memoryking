"""Worker control/dispatch tests use fixture vectors, never model-quality claims."""

import time
import unittest
from unittest.mock import Mock, patch

import test_sdk as sdk

from memoryking.adapters.local_embeddings import MODEL, LocalOnnxEmbedding, model_manifest
from memoryking.domain.embeddings import EmbeddingBatch, fingerprint
from memoryking.domain.local import LocalError
from memoryking.embedding_worker import run_batch, run_once


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
                    "model": MODEL,
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


class EmbeddingBatchWorkerTests(unittest.TestCase):
    setUp = sdk.SDKTests.setUp
    tearDown = sdk.SDKTests.tearDown
    provider = EmbeddingWorkerTests.provider

    def capture_extra(self, count):
        for n in range(count):
            self.client.capture(
                f"extra{n}",
                {
                    "version": 1,
                    "project": "p",
                    "source": "test",
                    "source_event": f"extra{n}",
                    "text": f"another observation {n}",
                    "readers": None,
                    "classification": "internal",
                    "observed_at_ms": 1,
                },
            )

    def fake_embed(self, provider):
        from memoryking.contracts.canonical import canonical_hash

        def embed(texts):
            return EmbeddingBatch(
                provider.fingerprint,
                384,
                [[1000000] + [0] * 383 for _ in texts],
                [canonical_hash(text) for text in texts],
                7 * len(texts),
                [False] * len(texts),
                [7] * len(texts),
            )

        return embed

    def test_one_claim_one_model_call_one_completion_for_a_batch(self):
        provider = self.provider()
        self.capture_extra(4)
        with (
            patch.object(provider, "embed", side_effect=self.fake_embed(provider)) as dispatch,
            patch.object(self.client, "embedding", wraps=self.client.embedding) as calls,
        ):
            result = run_batch(self.client, provider, 16)
            idle = run_batch(self.client, provider, 16)
        self.assertEqual((result["status"], result["count"], result["stored"]), ("complete", 5, 5))
        self.assertEqual(idle["status"], "idle")
        self.assertEqual(len(dispatch.call_args.args[0]), 5)
        dispatch.assert_called_once()
        self.assertEqual(
            [c.args[0]["action"] for c in calls.call_args_list],
            ["claim_batch", "complete_batch", "claim_batch"],
        )
        usage = self.store.db.execute("SELECT status,input_tokens FROM processing_usage").fetchall()
        self.assertEqual([tuple(row) for row in usage], [("succeeded", 7)] * 5)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM embedding_vectors").fetchone()[0], 5
        )

    def test_a_provider_failure_fails_every_job_of_the_batch_once(self):
        provider = self.provider()
        self.capture_extra(2)
        with patch.object(provider, "embed", side_effect=RuntimeError("secret")) as dispatch:
            result = run_batch(self.client, provider, 16)
        self.assertEqual((result["status"], result["failed"]), ("attempt_failed", 3))
        self.assertNotIn("secret", str(result))
        dispatch.assert_called_once()
        usage = self.store.db.execute("SELECT status,input_tokens FROM processing_usage").fetchall()
        self.assertEqual([tuple(row) for row in usage], [("failed", None)] * 3)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM embedding_vectors").fetchone()[0], 0
        )

    def test_a_record_purged_mid_batch_gets_no_vector_and_the_worker_goes_on(self):
        provider = self.provider()
        self.capture_extra(2)
        doomed = self.store.db.execute(
            "SELECT id FROM observations WHERE source_event='extra0'"
        ).fetchone()[0]
        real = self.fake_embed(provider)

        def embed(texts):
            self.store.purge(self.owner, doomed)  # while the model is running
            return real(texts)

        with patch.object(provider, "embed", side_effect=embed):
            result = run_batch(self.client, provider, 16)
        self.assertEqual(result["stored"], 0)
        self.assertEqual(result["refused"], 3)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM embedding_vectors").fetchone()[0], 0
        )
        self.assertNotIn(
            doomed,
            [r[0] for r in self.store.db.execute("SELECT subject FROM embedding_vectors")],
        )

    def test_a_lost_completion_reply_is_not_retried(self):
        provider = self.provider()
        original = self.client.embedding

        def transport(command):
            value = original(command)
            if command["action"] == "complete_batch":
                raise LocalError("transport_unavailable")
            return value

        with (
            patch.object(provider, "embed", side_effect=self.fake_embed(provider)) as dispatch,
            patch.object(self.client, "embedding", side_effect=transport),
        ):
            result = run_batch(self.client, provider, 16)
        self.assertEqual(result["status"], "reconciliation_required")
        dispatch.assert_called_once()

    def test_arbitrary_provider_rejected_before_batch_claim(self):
        client = Mock()
        with self.assertRaisesRegex(LocalError, "embedding_provider_unsupported"):
            run_batch(client, Mock())
        client.embedding.assert_not_called()
