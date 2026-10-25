"""Actual daemon/socket and provider-request inspection for delta host behavior."""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

import test_sdk as sdk
from test_working import checkpoint

from memoryking.harness_context import AuthorizedConversation, LocalRecordingProvider
from memoryking.resume_adapter import ResumeAdapter


class ResumeAdapterTests(unittest.TestCase):
    setUp = sdk.SDKTests.setUp
    tearDown = sdk.SDKTests.tearDown
    command = sdk.SDKTests.command
    save_command = sdk.SDKTests.save_command

    def options(self):
        return {
            "task": "task",
            "bindings": [{}],
            "max_output_tokens": 6000,
            "tokenizer": "cl100k_base",
        }

    def test_missing_client_base_reconstructs_full_and_counts_both_transfers(self):
        self.client.working(self.save_command())
        adapter = ResumeAdapter(self.client, "model_session")
        adapter.resume(**self.options())
        adapter._target["packet"]["checkpoint"]["state"]["objective"] = "corrupt local base"
        packet = adapter.resume(**self.options())
        self.assertEqual(packet["checkpoint"]["state"], checkpoint())
        self.assertEqual([item["mode"] for item in adapter.transfers], ["delta", "full"])
        self.assertTrue(all(item["bytes"] > 0 and item["tokens"] > 0 for item in adapter.transfers))

    def test_wrong_target_hash_refetches_once_and_never_passes_corrupt_state(self):
        self.client.working(self.save_command())
        adapter = ResumeAdapter(self.client, "model_session")
        original = self.client.working
        calls = []

        def tamper(command):
            result = original(command)
            calls.append(command)
            if len(calls) == 1:
                result["target_hash"] = "0" * 64
            return result

        with patch.object(self.client, "working", side_effect=tamper):
            packet = adapter.resume(**self.options())
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[1]["args"]["base"])
        self.assertEqual(packet["checkpoint"]["state"], checkpoint())

    def test_model_reset_retained_adapter_state_restores_every_essential_in_actual_request(self):
        self.client.working(self.save_command())
        provider = LocalRecordingProvider(self.root / "provider")
        conversation = AuthorizedConversation(self.client, provider)
        adapter = ResumeAdapter(self.client, conversation.model_session_id)
        conversation.add_memory(adapter.resume_for(conversation, **self.options()))
        first = conversation.dispatch()
        self.assertTrue(adapter._target)
        conversation.reset()
        self.assertTrue(adapter._target)  # Retention alone must not authorize reuse.
        packet = adapter.resume_for(conversation, **self.options())
        self.assertEqual(adapter.last_envelope["mode"], "full")
        conversation.add_memory(packet)
        second = conversation.dispatch()
        actual = json.loads(Path(second["request_path"]).read_bytes())
        self.assertEqual(actual["state_handles"], [])
        self.assertEqual(len(actual["messages"]), 1)
        self.assertEqual(
            json.loads(actual["messages"][0]["content"])["checkpoint"]["state"], checkpoint()
        )
        self.assertNotEqual(first["request_hash"], second["request_hash"])

    def test_permission_epoch_rebuild_excludes_old_memory_and_derived_handles(self):
        self.client.working(self.save_command())
        provider = LocalRecordingProvider(self.root / "provider")
        conversation = AuthorizedConversation(self.client, provider)
        adapter = ResumeAdapter(self.client, conversation.model_session_id)
        conversation.add_memory(adapter.resume_for(conversation, **self.options()))
        conversation.dispatch()
        # Conservative epoch invalidation must remove even an unrelated retained
        # provider handle; the next exact request contains the newly checked packet.
        self.store.grant(self.owner, "worker", "p", ["read"])
        self.store.revoke(self.owner, "worker", "p")
        packet = adapter.resume_for(conversation, **self.options())
        self.assertEqual(adapter.last_envelope["mode"], "full")
        conversation.add_memory(packet)
        result = conversation.dispatch()
        actual = json.loads(Path(result["request_path"]).read_bytes())
        self.assertEqual(actual["state_handles"], [])
        self.assertEqual(len(actual["messages"]), 1)
        self.assertEqual(json.loads(actual["messages"][0]["content"])["receipt"], packet["receipt"])
