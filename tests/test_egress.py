"""Provider adapters leave the machine only under the daemon's egress policy, audited."""

import hashlib
import http.server
import json
import threading
import unittest

import test_inference

from memoryking.adapters.egress_policy import EgressPolicy
from memoryking.adapters.inference import Inference
from memoryking.adapters.local_files import atomic
from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import LocalError
from memoryking.harness_context import AuthorizedConversation, LocalRecordingProvider
from memoryking.providers import adapter

TOKENS = {"input_tokens": 5, "output_tokens": 2}


class FakeProvider(http.server.BaseHTTPRequestHandler):
    """Answers like each real provider; records the exact bytes it was sent."""

    requests: list[dict] = []

    def log_message(self, *args):  # silence
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        FakeProvider.requests.append(
            {"path": self.path, "headers": dict(self.headers), "body": body}
        )
        parsed = json.loads(body)
        model = parsed.get("model") or self.path.split("/models/")[-1].split(":")[0]
        text = "reply to " + model
        if self.path == "/fail":
            self.send_response(500)
            self.end_headers()
            return
        if self.path == "/v1/messages" or self.path.endswith(":rawPredict"):
            reply = {"content": [{"type": "text", "text": text}], "usage": TOKENS}
        elif self.path.endswith(":generateContent"):
            reply = {
                "candidates": [{"content": {"parts": [{"text": text}]}}],
                "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2},
            }
        elif self.path == "/huge":
            reply = {"choices": [{"message": {"content": "x" * 70000}}]}
        else:
            reply = {
                "choices": [{"message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            }
        raw = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class EgressCases:
    """Shared by the SQLite socket fixture and the native PostgreSQL fixture."""

    def policy_toml(self, revision="test-2026", openai_ceiling="internal", openai_path=None):
        host = f"127.0.0.1:{self.provider_port}"
        key = self.root / "secrets" / "key.txt"
        token = self.root / "secrets" / "token.txt"
        if not key.exists():
            (self.root / "secrets").mkdir(mode=0o700, exist_ok=True)
            atomic(key, b"sk-test-key\n")
            atomic(token, b"ya29.test-token\n")
        path_line = f'path = "{openai_path}"\n' if openai_path else ""
        return f"""revision = "{revision}"
[[providers]]
name = "anthropic-messages"
host = "{host}"
insecure_http = true
models = ["claude-sonnet-5-5"]
key_file = "{key}"
max_classification = "restricted"
[[providers]]
name = "openai-chat"
host = "{host}"
insecure_http = true
models = ["llama3.1:8b", "grok-4"]
max_classification = "{openai_ceiling}"
{path_line}[[providers]]
name = "vertex-ai"
host = "{host}"
insecure_http = true
location = "us-central1"
project = "demo-project"
models = ["gemini-2.5-pro"]
token_file = "{token}"
"""

    def load_policy(self, **kw):
        text = self.policy_toml(**kw)
        path = self.root / "secrets" / "egress.toml"
        atomic(path, text.encode())
        policy = EgressPolicy.load(path)
        self.set_policy(policy)
        return policy

    def conversation(self, policy, name, model):
        return AuthorizedConversation(self.client, adapter(policy, name, model))

    def audit(self, before=None):
        return self.client.inference_status(
            {"version": 1, "action": "audit", "args": {"project": "p", "before": before}}
        )

    def test_policy_parsing_rejects_unsafe_and_malformed_entries(self):
        good = {
            "revision": "r1",
            "providers": [{"name": "openai-chat", "host": "api.openai.com", "models": ["gpt-5"]}],
        }
        policy = EgressPolicy.parse(good)
        self.assertEqual(policy.provider("openai-chat").max_classification, "internal")
        self.assertEqual(policy.provider("openai-chat").max_request_bytes, 98304)
        bad = [
            {**good, "revision": "local-recording-v1"},
            {**good, "revision": "bad revision"},
            {**good, "extra": 1},
            {**good, "providers": [{**good["providers"][0], "insecure_http": True}]},
            {**good, "providers": [{**good["providers"][0], "name": "mystery"}]},
            {**good, "providers": [{**good["providers"][0], "models": []}]},
            {**good, "providers": [{**good["providers"][0], "models": ["bad model"]}]},
            {**good, "providers": [{**good["providers"][0], "max_classification": "secret"}]},
            {**good, "providers": [{**good["providers"][0], "max_request_bytes": 10**9}]},
            {**good, "providers": [{**good["providers"][0], "key_file": "relative.key"}]},
            {**good, "providers": [{**good["providers"][0], "headers": {"Authorization": "x"}}]},
            {**good, "providers": [{**good["providers"][0], "token_command": ["gcloud"]}]},
            {**good, "providers": [good["providers"][0], good["providers"][0]]},
            {"revision": "r1", "providers": [{"name": "vertex-ai", "models": ["gemini-2.5-pro"]}]},
        ]
        for value in bad:
            with self.assertRaises(LocalError) as caught:
                EgressPolicy.parse(value)
            self.assertEqual(caught.exception.code, "egress_policy_invalid", value)
        loopback = {
            "revision": "r1",
            "providers": [
                {
                    "name": "openai-chat",
                    "host": "localhost:11434",
                    "insecure_http": True,
                    "models": ["llama3.1:8b"],
                }
            ],
        }
        self.assertTrue(EgressPolicy.parse(loopback).provider("openai-chat").insecure_http)
        vertex = EgressPolicy.parse(
            {
                "revision": "r1",
                "providers": [
                    {
                        "name": "vertex-ai",
                        "location": "europe-west4",
                        "project": "demo-project",
                        "models": ["gemini-2.5-pro"],
                        "token_command": ["/usr/bin/gcloud", "auth", "print-access-token"],
                    }
                ],
            }
        )
        self.assertEqual(
            vertex.provider("vertex-ai").host, "europe-west4-aiplatform.googleapis.com"
        )

    def test_without_a_policy_only_the_local_recorder_is_accepted(self):
        policy = EgressPolicy.parse(
            {
                "revision": "r1",
                "providers": [
                    {
                        "name": "openai-chat",
                        "host": "127.0.0.1:1",
                        "insecure_http": True,
                        "models": ["grok-4"],
                    }
                ],
            }
        )
        with self.assertRaises(LocalError) as caught:
            self.conversation(policy, "openai-chat", "grok-4")
        self.assertEqual(caught.exception.code, "provider_not_supported")
        with self.assertRaises(LocalError) as caught:
            adapter(policy, "openai-chat", "gpt-5")
        self.assertEqual(caught.exception.code, "egress_provider_denied")
        with self.assertRaises(LocalError):
            AuthorizedConversation(self.client, object())  # type: ignore[arg-type]
        # The local recorder keeps working, and leaves no audit record.
        local = AuthorizedConversation(self.client, LocalRecordingProvider(self.root / "rec"))
        self.host(context=local)
        self.assertIn("Local echo", local.dispatch()["content"])
        self.assertEqual(self.audit()["items"], [])

    def test_each_adapter_sends_exact_bytes_and_every_delivery_is_audited(self):
        policy = self.load_policy()
        FakeProvider.requests.clear()
        cases = [
            ("anthropic-messages", "claude-sonnet-5-5", "/v1/messages"),
            ("openai-chat", "llama3.1:8b", "/v1/chat/completions"),
            (
                "vertex-ai",
                "gemini-2.5-pro",
                "/v1/projects/demo-project/locations/us-central1/publishers/google/models/gemini-2.5-pro:generateContent",
            ),
        ]
        for name, model, path in cases:
            conversation = self.conversation(policy, name, model)
            self.host("Summarize the current task in one line", context=conversation)
            result = conversation.dispatch()
            self.assertEqual(result["content"], "reply to " + model)
            self.assertEqual(result["status"], "validated")
            self.assertEqual(result["usage"], TOKENS)
            sent = FakeProvider.requests[-1]
            self.assertEqual(sent["path"], path)
            sent_body = json.loads(sent["body"])
            self.assertEqual(sent["body"], canonical_bytes(sent_body))
            self.assertEqual(result["wire_hash"], hashlib.sha256(sent["body"]).hexdigest())
            if name == "anthropic-messages":
                self.assertEqual(sent["headers"]["x-api-key"], "sk-test-key")
                self.assertEqual(sent_body["messages"][0]["role"], "user")
                self.assertEqual(sent_body["max_tokens"], 1024)
            if name == "openai-chat":
                self.assertNotIn("Authorization", sent["headers"])
                self.assertEqual(sent_body["messages"][0]["role"], "user")
            if name == "vertex-ai":
                self.assertEqual(sent["headers"]["Authorization"], "Bearer ya29.test-token")
                self.assertEqual(sent_body["contents"][0]["role"], "user")
                self.assertEqual(sent_body["generationConfig"], {"maxOutputTokens": 1024})
            # The reply is now part of the conversation; a second dispatch resends it all.
            self.host("Continue", context=conversation)
            conversation.dispatch()
            self.assertEqual(
                len(
                    json.loads(FakeProvider.requests[-1]["body"])[
                        "messages" if name != "vertex-ai" else "contents"
                    ]
                ),
                3,
            )
        listing = self.audit()
        self.assertEqual(listing["policy_revision"], "test-2026")
        self.assertEqual(len(listing["items"]), 6)
        newest = listing["items"][0]
        self.assertEqual(newest["status"], "delivered")
        self.assertEqual(newest["provider"], "vertex-ai")
        self.assertEqual(newest["host"], f"127.0.0.1:{self.provider_port}")
        self.assertEqual(newest["classification"], "internal")
        self.assertEqual((newest["input_tokens"], newest["output_tokens"]), (5, 2))
        self.assertEqual(newest["segments"], 3)
        self.assertEqual(newest["receipts"], [])
        self.assertEqual(
            newest["wire_hash"], hashlib.sha256(FakeProvider.requests[-1]["body"]).hexdigest()
        )
        self.assertEqual(
            newest["response_hash"], hashlib.sha256(b"reply to gemini-2.5-pro").hexdigest()
        )
        self.assertNotIn("Summarize", json.dumps(listing))
        self.assertNotIn("sk-test-key", json.dumps(listing))
        page = self.audit(before=listing["items"][2]["evaluated_ms"])
        self.assertLessEqual(len(page["items"]), 3)
        self.assertTrue(
            all(i["evaluated_ms"] < listing["items"][2]["evaluated_ms"] for i in page["items"])
        )

    def test_vertex_anthropic_publisher_uses_raw_predict(self):
        policy = self.load_policy()
        toml = self.policy_toml().replace(
            'models = ["gemini-2.5-pro"]', 'models = ["claude-sonnet-5-5"]\npublisher = "anthropic"'
        )
        path = self.root / "secrets" / "egress-vertex.toml"
        atomic(path, toml.encode())
        policy = EgressPolicy.load(path)
        self.set_policy(policy)
        conversation = self.conversation(policy, "vertex-ai", "claude-sonnet-5-5")
        self.host(context=conversation)
        self.assertEqual(conversation.dispatch()["content"], "reply to claude-sonnet-5-5")
        sent = FakeProvider.requests[-1]
        self.assertTrue(
            sent["path"].endswith("/publishers/anthropic/models/claude-sonnet-5-5:rawPredict")
        )
        body = json.loads(sent["body"])
        self.assertEqual(body["anthropic_version"], "vertex-2023-10-16")
        self.assertNotIn("model", body)

    def test_restricted_memory_is_refused_below_the_ceiling_before_any_permit(self):
        policy = self.load_policy()
        restricted = "DISPOSABLE_RESTRICTED_FACT"
        source = self.client.capture(
            "restricted-1",
            {
                "version": 1,
                "project": "p",
                "source": "test",
                "source_event": "restricted-1",
                "text": restricted,
                "readers": ["owner"],
                "classification": "restricted",
                "observed_at_ms": 1,
            },
        )
        packet = self.fetch(source["observation"], "observation", restricted)
        FakeProvider.requests.clear()
        low = self.conversation(policy, "openai-chat", "grok-4")
        low.add_memory(packet)
        with self.assertRaises(LocalError) as caught:
            low.dispatch()
        self.assertEqual(caught.exception.code, "egress_classification_denied")
        self.assertEqual(FakeProvider.requests, [])
        self.assertEqual(self.audit()["items"], [])
        high = self.conversation(policy, "anthropic-messages", "claude-sonnet-5-5")
        high.add_memory(packet)
        self.assertEqual(high.dispatch()["status"], "validated")
        record = self.audit()["items"][0]
        self.assertEqual(record["classification"], "restricted")
        self.assertEqual(record["receipts"], [packet["receipt"]])
        self.assertIn(restricted, FakeProvider.requests[-1]["body"].decode())
        # A claim resting on restricted evidence inherits the ceiling of its sources.
        proposed = self.propose(
            self.body(
                "derived from restricted",
                evidence=[
                    {
                        "observation": source["observation"],
                        "start_byte": 0,
                        "end_byte": len(restricted.encode()),
                        "content_hash": hashlib.sha256(restricted.encode()).hexdigest(),
                    }
                ],
            ),
            key="derived",
        )
        self.review(proposed)
        claim = self.fetch(proposed["revision"], "revision", "derived from restricted")
        low = self.conversation(policy, "openai-chat", "grok-4")
        low.add_memory(claim)
        with self.assertRaises(LocalError) as caught:
            low.dispatch()
        self.assertEqual(caught.exception.code, "egress_classification_denied")
        # A host message above the ceiling is refused the same way.
        low = self.conversation(policy, "openai-chat", "grok-4")
        self.host("restricted instruction", context=low, classification="restricted")
        with self.assertRaises(LocalError) as caught:
            low.dispatch()
        self.assertEqual(caught.exception.code, "egress_classification_denied")

    def test_provider_failure_is_reported_and_nothing_is_admitted(self):
        policy = self.load_policy(openai_path="/fail")
        conversation = self.conversation(policy, "openai-chat", "grok-4")
        self.host(context=conversation)
        with self.assertRaises(LocalError) as caught:
            conversation.dispatch()
        self.assertEqual(caught.exception.code, "provider_unavailable")
        record = self.audit()["items"][0]
        self.assertEqual(record["status"], "failed")
        self.assertIsNone(record["wire_hash"])
        with self.assertRaises(LocalError) as caught:
            conversation.dispatch()
        self.assertEqual(caught.exception.code, "context_rebuild_required")
        oversized = self.load_policy(openai_path="/huge")
        conversation = self.conversation(oversized, "openai-chat", "grok-4")
        self.host(context=conversation)
        with self.assertRaises(LocalError) as caught:
            conversation.dispatch()
        self.assertEqual(caught.exception.code, "provider_unavailable")
        self.assertEqual(self.audit()["items"][0]["status"], "failed")

    def test_policy_change_retires_every_conversation(self):
        policy = self.load_policy()
        conversation = self.conversation(policy, "openai-chat", "grok-4")
        self.host(context=conversation)
        self.load_policy(revision="test-2027")
        with self.assertRaises(LocalError) as caught:
            conversation.dispatch()
        self.assertEqual(caught.exception.code, "provider_not_supported")
        self.set_policy(None)
        with self.assertRaises(LocalError) as caught:
            self.conversation(policy, "openai-chat", "grok-4")
        self.assertEqual(caught.exception.code, "provider_not_supported")

    def test_report_is_once_owner_bound_and_a_dead_host_leaves_pending(self):
        policy = self.load_policy()
        conversation = self.conversation(policy, "openai-chat", "grok-4")
        self.host(context=conversation)
        binding = conversation._sync()
        report = {
            "version": 1,
            "action": "report",
            "args": {
                "binding": binding,
                "permit": "permit_" + "0" * 32,
                "status": "delivered",
                "wire_hash": None,
                "response_hash": None,
                "usage": None,
            },
        }
        with self.assertRaises(LocalError) as caught:
            self.client.inference_status(report)
        self.assertEqual(caught.exception.code, "egress_audit_missing")
        # Lose the report: the record stays pending, truthfully.
        original = conversation._report
        conversation._report = lambda *a, **k: None
        conversation.dispatch()
        record = self.audit()["items"][0]
        self.assertEqual(record["status"], "pending")
        conversation._report = original
        report["args"]["permit"] = record["permit"]
        report["args"]["binding"] = conversation._sync()
        self.assertEqual(self.client.inference_status(report)["outcome"], "delivered")
        with self.assertRaises(LocalError) as caught:
            self.client.inference_status(report)
        self.assertEqual(caught.exception.code, "egress_audit_reported")
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        with self.assertRaises(LocalError):
            Inference(self.store).execute(
                reader, {"version": 1, "action": "audit", "args": {"project": "p", "before": None}}
            )

    def test_audit_records_outlive_purge_and_expire_with_retention(self):
        policy = self.load_policy()
        packet = self.fetch()
        conversation = self.conversation(policy, "openai-chat", "grok-4")
        conversation.add_memory(packet)
        conversation.dispatch()
        self.assertEqual(self.audit()["items"][0]["receipts"], [packet["receipt"]])
        self.store.purge(self.owner, self.obs["observation"])
        self.assertEqual(len(self.audit()["items"]), 1)
        clock = self.clock()
        clock.utc += 31 * 24 * 3600 * 1000
        self.store.gc()
        self.assertEqual(self.audit()["items"], [])


class RenderTests(unittest.TestCase):
    def test_cli_table_shortens_nothing_but_shows_every_record(self):
        from memoryking.__main__ import render_audit

        empty = render_audit({"policy_revision": "r1", "items": [], "next_before": None})
        self.assertIn("no egress records", empty)
        text = render_audit(
            {
                "policy_revision": "r1",
                "next_before": 5,
                "items": [
                    {
                        "evaluated_ms": 0,
                        "status": "delivered",
                        "provider": "openai-chat",
                        "model": "grok-4",
                        "host": "api.x.ai",
                        "classification": "internal",
                        "request_bytes": 1234,
                        "receipts": ["rcpt_1", "rcpt_2"],
                        "permit": "permit_" + "a" * 32,
                    }
                ],
            }
        )
        self.assertIn("1970-01-01 00:00:00  delivered  openai-chat  grok-4  api.x.ai", text)
        self.assertIn("more: --before 5", text)


class EgressTests(EgressCases, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
        cls.provider_port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    setUp = test_inference.InferenceTests.setUp
    tearDown = test_inference.InferenceTests.tearDown
    host = test_inference.InferenceTests.host
    fetch = test_inference.InferenceTests.fetch
    propose = test_inference.InferenceTests.propose
    body = test_inference.InferenceTests.body
    review = test_inference.InferenceTests.review
    run_k = test_inference.InferenceTests.run_k
    clock = test_inference.InferenceTests.clock

    def set_policy(self, policy):
        self.store.egress_policy = policy


if __name__ == "__main__":
    unittest.main()
