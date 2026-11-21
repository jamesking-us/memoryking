"""`memoryking consolidate` through a real daemon socket, the CLI and a loopback provider."""

import http.server
import json
import threading
import unittest

from test_review_cli import DaemonCase

from memoryking.adapters.egress_policy import EgressPolicy
from memoryking.adapters.local_files import atomic


class Provider(http.server.BaseHTTPRequestHandler):
    """An OpenAI-compatible endpoint that quotes the first observation it is shown."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        packet = json.loads(body["messages"][0]["content"])
        first = packet["observations"][0]
        quote = " ".join(first["text"].split()[:5])
        claims = {
            "claims": [
                {
                    "text": "Drafted from " + quote,
                    "kind": "fact",
                    "evidence": [{"observation": first["id"], "quote": quote}],
                }
            ]
        }
        raw = json.dumps(
            {
                "choices": [{"message": {"role": "assistant", "content": json.dumps(claims)}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class ConsolidateCliTests(DaemonCase):
    def setUp(self):
        super().setUp()
        self.provider = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        threading.Thread(target=self.provider.serve_forever, daemon=True).start()
        secrets = self.base / "secrets"
        secrets.mkdir(mode=0o700)
        self.policy_file = secrets / "egress.toml"
        atomic(
            self.policy_file,
            f"""revision = "cli-test"
[[providers]]
name = "openai-chat"
host = "127.0.0.1:{self.provider.server_address[1]}"
insecure_http = true
models = ["grok-4"]
""".encode(),
        )

    def tearDown(self):
        self.provider.shutdown()
        self.provider.server_close()
        super().tearDown()

    def test_it_refuses_without_a_policy_and_the_local_recorder_only_dry_runs(self):
        self.capture("a", "The release train leaves every Thursday afternoon.")
        refused = self.cli("consolidate", "--project", "p", expect=2)
        self.assertIn("egress_policy_required", refused)
        refused = self.cli(
            "consolidate",
            "--project",
            "p",
            "--provider",
            "openai-chat",
            "--model",
            "grok-4",
            expect=2,
        )
        self.assertIn("egress_policy_required", refused)
        dry = json.loads(
            self.cli("consolidate", "--project", "p", "--provider", "local-recording", "--json")
        )
        self.assertEqual(dry["status"], "not_admitted")
        self.assertEqual(dry["observations"], 1)
        self.assertNotIn("Drafted", self.cli("review", "list", "--project", "p"))

    def test_a_pass_through_the_policy_files_a_proposal_and_a_second_finds_nothing(self):
        self.store.egress_policy = EgressPolicy.load(self.policy_file)
        self.capture("a", "The release train leaves every Thursday afternoon.")
        args = (
            "--project",
            "p",
            "--egress-policy",
            str(self.policy_file),
            "--provider",
            "openai-chat",
            "--model",
            "grok-4",
        )
        first = json.loads(self.cli("consolidate", *args, "--json"))
        self.assertEqual(first["status"], "admitted")
        self.assertEqual(len(first["filed"]), 1)
        text = self.cli("review", "list", "--project", "p")
        self.assertIn("Drafted from The release train leaves every", text)
        second = json.loads(self.cli("consolidate", *args, "--json"))
        self.assertEqual(second["status"], "nothing_new")
        self.assertIn("delivered", self.cli("egress-audit", "--project", "p"))
        self.assertIn("Nothing new to consolidate", self.cli("consolidate", *args, "--since", "1d"))

    def test_the_unreviewed_tier_is_an_admin_switch_and_labels_what_recall_shows(self):
        self.capture("a", "Retries in payments/retry.py are capped at three attempts.")
        self.capture("b", "The payments/retry.py cap of three attempts caused a page last week.")
        self.cli("draft-proposals", "--project", "p", "--no-procedures")
        ask = ("recall", "--project", "p", "--query", "payments retry attempts")
        self.assertNotIn("Unreviewed", self.cli(*ask))
        self.assertIn("off", self.cli("unreviewed", "show", "--project", "p"))
        reviewer = self.credential("rev", ["read", "review", "capture"])
        refused = self.cli("unreviewed", "on", "--project", "p", credential=reviewer, expect=2)
        self.assertIn("denied", refused)
        self.assertIn("ON", self.cli("unreviewed", "on", "--project", "p"))
        shown = self.cli(*ask)
        self.assertIn("## Unreviewed, drafted from evidence", shown)
        self.assertIn("repeated evidence", shown)
        self.assertNotIn("## Must follow", shown)
        self.assertEqual(
            json.loads(self.cli("unreviewed", "show", "--project", "p", "--json"))["enabled"], True
        )
        self.cli("unreviewed", "off", "--project", "p")
        self.assertNotIn("Unreviewed", self.cli(*ask))


if __name__ == "__main__":
    unittest.main()
