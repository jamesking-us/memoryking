"""The dependency-free function-calling helper, against a real daemon socket."""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from memoryking import lean
from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
from memoryking.domain.local import LocalError
from memoryking.integrations.function_calling import EVERYDAY, REVIEW, FunctionTools
from memoryking.sdk import LocalClient


class FunctionCallingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-fc-")
        root = Path(self.temp.name)
        self.credential = LocalStore.initialize(root / "data", root / "control")
        self.store = LocalStore(root / "data", root / "control")
        self.server = Server(self.store)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.client = LocalClient(self.server.path, self.credential, "checkout")
        self.tools = FunctionTools(self.client)

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.store.close()
        self.temp.cleanup()

    def test_definitions_are_the_lean_catalog(self):
        chat = self.tools.definitions()
        self.assertEqual([item["function"]["name"] for item in chat], list(EVERYDAY))
        catalog = {item["name"]: item for item in lean.TOOLS}
        for item in chat:
            self.assertEqual(item["type"], "function")
            function = item["function"]
            self.assertEqual(function["parameters"], catalog[function["name"]]["inputSchema"])
            self.assertEqual(function["description"], catalog[function["name"]]["description"])
            Draft202012Validator.check_schema(function["parameters"])
        flat = self.tools.definitions("responses")
        self.assertEqual(flat[0]["name"], "remember")
        self.assertNotIn("function", flat[0])
        json.dumps(chat)

    def test_definitions_are_copies(self):
        self.tools.definitions()[0]["function"]["parameters"]["required"].append("x")
        required = self.tools.definitions()[0]["function"]["parameters"]["required"]
        self.assertEqual(required, ["text"])

    def test_the_five_operations_run_as_a_model_would_call_them(self):
        saved = json.loads(
            self.tools.call("remember", '{"text": "Raising every timeout broke the deadline"}')
        )
        self.assertEqual(saved["held"], False)
        self.assertEqual(saved["position"], 1)
        briefing = self.tools.call("recall", json.dumps({"query": "timeout deadline"}))
        self.assertIn("# Briefing", briefing)
        self.assertIn(saved["observation"], briefing)
        progress = json.loads(
            self.tools.call(
                "save_progress",
                json.dumps(
                    {
                        "task": "retry-fix",
                        "objective": "Fix the retry deadline",
                        "current_state": "Investigating",
                        "next_actions": ["Run the regression"],
                        "share_with": ["reviewer"],
                    }
                ),
            )
        )
        self.assertEqual(progress["revision"], 1)
        self.assertIn("Fix the retry deadline", self.tools.call("resume", '{"task": "retry-fix"}'))
        self.store.grant(self.credential.read_text().strip(), "reviewer", "checkout", ["read"])
        handed = json.loads(self.tools.call("hand_off", '{"task": "retry-fix", "to": "reviewer"}'))
        self.assertTrue(handed["handle"])

    def test_results_match_the_client_methods(self):
        self.client.remember("same text", source="pytest")
        arguments = '{"text": "same text", "source": "pytest"}'
        via_tool = json.loads(self.tools.call("remember", arguments))
        via_client = self.client.remember("same text", source="pytest")
        self.assertEqual(via_tool["observation"], via_client.observation)

    def test_refusals_are_text_with_a_code_and_a_hint(self):
        for name, arguments in (
            ("recall", "{}"),
            ("recall", "not json"),
            ("recall", '["a list"]'),
            ("remember", '{"text": "x", "extra": 1}'),
            ("forget", '{"memory": "m", "why": "w"}'),  # not enabled for this tool set
            ("nope", "{}"),
        ):
            reply = json.loads(self.tools.call(name, arguments))
            self.assertEqual(set(reply), {"error", "hint"}, (name, arguments))
            self.assertTrue(reply["hint"])
        self.assertEqual(
            json.loads(self.tools.call("resume", '{"task": "never-saved"}'))["error"],
            "checkpoint_unavailable",
        )
        self.assertTrue(self.tools.execute("recall", {})[1])

    def test_review_tools_are_opt_in_and_confirm_needs_the_operator(self):
        with self.assertRaises(LocalError):
            FunctionTools(self.client, ("remember", "nonexistent"))
        review = FunctionTools(self.client, (*EVERYDAY, *REVIEW))
        self.assertEqual(len(review.definitions()), 9)
        saved = json.loads(review.call("remember", '{"text": "an observation"}'))
        proposed = json.loads(
            review.call(
                "propose", json.dumps({"text": "A claim", "evidence": [saved["observation"]]})
            )
        )
        self.assertIn("proposal", proposed)
        refused = json.loads(
            review.call(
                "confirm",
                json.dumps(
                    {"proposal": proposed["proposal"], "decision": "accept", "user_said": "yes"}
                ),
            )
        )
        self.assertEqual(refused["error"], "chat_review_disabled")

    def test_a_dead_daemon_is_a_refusal_not_a_crash(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        try:
            reply = json.loads(self.tools.call("recall", '{"query": "x"}'))
            self.assertEqual(reply["error"], "transport_unavailable")
        finally:
            self.server = Server(self.store)
            self.thread = threading.Thread(target=self.server.serve_forever)
            self.thread.start()


if __name__ == "__main__":
    unittest.main()
