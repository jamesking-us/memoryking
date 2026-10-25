"""Real local/shared SDK requests and actual reference-provider input inspection."""

import unittest
from unittest.mock import patch

import test_sdk as sdk
import test_shared_http as shared

from memoryking.adapters.local_embeddings import LocalOnnxEmbedding, model_manifest
from memoryking.domain.embeddings import fingerprint
from memoryking.domain.local import SystemClock
from memoryking.embedding_worker import run_once
from tools.processing_demo import exercise


class LocalProcessingTransportTests(unittest.TestCase):
    setUp = sdk.SDKTests.setUp
    tearDown = sdk.SDKTests.tearDown

    def test_harmful_candidate_cannot_rewrite_actual_provider_input(self):
        self.assertTrue(exercise(self.client, self.root)["passed"])


class SharedProcessingTransportTests(unittest.TestCase):
    clock_factory = SystemClock
    setUpClass = classmethod(shared.SharedHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(shared.SharedHTTPTests.tearDownClass.__func__)
    store = shared.SharedHTTPTests.store
    setUp = shared.SharedHTTPTests.setUp
    tearDown = shared.SharedHTTPTests.tearDown

    def test_harmful_candidate_cannot_rewrite_actual_provider_input(self):
        self.assertTrue(exercise(self.client, self.files)["passed"])

    def test_shared_provider_outage_keeps_lexical_path_and_unknown_usage(self):
        self.client.capture(
            "outage",
            {
                "version": 1,
                "project": "demo",
                "source": "fixture",
                "source_event": "outage",
                "text": "Timeout failed",
                "readers": None,
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )
        provider = object.__new__(LocalOnnxEmbedding)
        provider.fingerprint = fingerprint(model_manifest())
        provider.dimensions = 384
        self.client.embedding(
            {
                "version": 1,
                "incarnation": self.client.binding[-1],
                "action": "configure",
                "key": "worker-config",
                "args": {
                    "project": "demo",
                    "model": "minilm-l6-v2-onnx",
                    "state": "experimental",
                    "expected_generation": 0,
                },
            }
        )
        with patch.object(provider, "embed", side_effect=OSError("synthetic outage")) as dispatch:
            result = run_once(self.client, provider)
        self.assertEqual(result["status"], "attempt_failed")
        dispatch.assert_called_once()
        packet = self.client.retrieval(
            {
                "version": 1,
                "incarnation": self.client.binding[-1],
                "action": "brief",
                "args": {
                    "project": "demo",
                    "query": "Timeout",
                    "bindings": [{}],
                    "max_output_tokens": 6000,
                    "tokenizer": "cl100k_base",
                },
            }
        )
        self.assertTrue(packet["guidance"])
        usage = self.client.processing(
            {
                "version": 1,
                "incarnation": self.client.binding[-1],
                "action": "usage",
                "args": {"project": "demo", "successful_tasks": 0},
            }
        )
        self.assertEqual(usage["statuses"]["failed"], 1)
        self.assertIsNone(usage["total_tokens"])
