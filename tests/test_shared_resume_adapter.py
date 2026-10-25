"""Shared HTTP, native PostgreSQL and actual full provider requests after deltas."""

import json
import unittest
from pathlib import Path

import test_shared_http as shared
from test_working import checkpoint

from memoryking.domain.local import SystemClock
from memoryking.harness_context import AuthorizedConversation, LocalRecordingProvider
from memoryking.resume_adapter import ResumeAdapter


class SharedResumeAdapterTests(unittest.TestCase):
    clock_factory = SystemClock
    setUpClass = classmethod(shared.SharedHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(shared.SharedHTTPTests.tearDownClass.__func__)
    store = shared.SharedHTTPTests.store
    setUp = shared.SharedHTTPTests.setUp
    tearDown = shared.SharedHTTPTests.tearDown

    def save(self):
        self.client.working(
            {
                "version": 1,
                "incarnation": self.client.binding[-1],
                "action": "save",
                "key": "save",
                "args": {
                    "project": "demo",
                    "task": "task",
                    "run": None,
                    "expected_revision": 0,
                    "readers": ["owner"],
                    "checkpoint": checkpoint(),
                },
            }
        )

    def options(self):
        return {
            "task": "task",
            "bindings": [{}],
            "max_output_tokens": 6000,
            "tokenizer": "cl100k_base",
        }

    def test_missing_base_shared_sdk_reconstructs_and_counts_full_fallback(self):
        self.save()
        adapter = ResumeAdapter(self.client, "model_session")
        adapter.resume(**self.options())
        adapter._target["packet"]["checkpoint"]["state"]["objective"] = "corrupt local base"
        packet = adapter.resume(**self.options())
        self.assertEqual(packet["checkpoint"]["state"], checkpoint())
        self.assertEqual([item["mode"] for item in adapter.transfers], ["delta", "full"])
        self.assertTrue(all(item["bytes"] > 0 and item["tokens"] > 0 for item in adapter.transfers))

    def test_model_reset_with_retained_base_has_full_essentials_in_actual_provider_request(self):
        self.save()
        provider = LocalRecordingProvider(self.files / "provider")
        conversation = AuthorizedConversation(self.client, provider)
        adapter = ResumeAdapter(self.client, conversation.model_session_id)
        first = adapter.resume_for(conversation, **self.options())
        conversation.add_memory(first)
        conversation.dispatch()
        conversation.reset()
        self.assertTrue(adapter._target)
        resumed = adapter.resume_for(conversation, **self.options())
        self.assertEqual(adapter.last_envelope["mode"], "full")
        conversation.add_memory(resumed)
        result = conversation.dispatch()
        actual = json.loads(Path(result["request_path"]).read_bytes())
        self.assertEqual(actual["state_handles"], [])
        self.assertEqual(len(actual["messages"]), 1)
        self.assertEqual(
            json.loads(actual["messages"][0]["content"])["checkpoint"]["state"], checkpoint()
        )
