"""Review, corrections and the session start through a real daemon, the CLI and the bridge."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from test_sdk import initialize, rpc

from memoryking import lean
from memoryking.adapters.local_files import atomic
from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import LocalError
from memoryking.sdk import LocalClient


class DaemonCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mk-rv-", dir="/tmp")
        self.base = Path(self.temp.name)
        self.owner_file = LocalStore.initialize(self.base / "store", self.base / "control")
        self.store = LocalStore(self.base / "store", self.base / "control")
        self.owner = self.owner_file.read_text()
        self.server = Server(self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORYKING_")}

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.store.close()
        self.temp.cleanup()

    def credential(self, principal, capabilities, project="p"):
        token = self.store.grant(self.owner, principal, project, capabilities)
        path = self.base / f"{principal}.credential"
        atomic(path, token.encode())
        return path

    def cli(self, *args, credential=None, expect=0, stdin=None):
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "memoryking",
                *args,
                "--socket",
                str(self.server.path),
                "--credential",
                str(credential or self.owner_file),
            ],
            capture_output=True,
            text=True,
            input=stdin,
            env=self.env,
            timeout=60,
        )
        self.assertEqual(run.returncode, expect, run.stdout + run.stderr)
        return run.stdout if expect == 0 else run.stderr

    def capture(self, key, text, source="cli", credential=None, *extra):
        out = self.cli(
            "capture",
            "--project",
            "p",
            "--text",
            text,
            "--source",
            source,
            "--event",
            key,
            *extra,
            credential=credential,
        )
        return json.loads(out)["observation"]

    def bridge(self, calls, credential=None, *extra):
        frames = [initialize(), {"jsonrpc": "2.0", "method": "notifications/initialized"}]
        frames += [
            rpc(10 + n, "tools/call", {"name": name, "arguments": arguments})
            for n, (name, arguments) in enumerate(calls)
        ]
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "memoryking.adapters.stdio",
                "--socket",
                str(self.server.path),
                "--credential",
                str(credential or self.owner_file),
                "--project",
                "p",
                "--profile",
                "lean",
                *extra,
            ],
            input=b"".join(canonical_bytes(f) + b"\n" for f in frames),
            capture_output=True,
            timeout=60,
            env=self.env,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        replies = {
            r["id"]: r.get("result", r)
            for r in map(json.loads, run.stdout.splitlines())
            if "id" in r
        }
        return [replies[10 + n] for n in range(len(calls))]

    @staticmethod
    def payload(reply):
        """The machine envelope of a lean reply, with a briefing's Markdown as `text`."""
        if "structuredContent" not in reply:
            return json.loads(reply["content"][0]["text"])
        value = dict(reply["structuredContent"])
        if value.get("format") == "text":
            value["text"] = reply["content"][0]["text"]
        return value


class ReviewCliTests(DaemonCase):
    def two_notes(self, topic="payments/retry.py", suffix=""):
        self.capture(f"a{suffix}", f"Scoped the deadline per request in {topic} yesterday.")
        self.capture(f"b{suffix}", f"Review note: {topic} now retries once per request.")

    def waiting(self):
        return json.loads(self.cli("review", "list", "--project", "p", "--json"))

    def test_list_show_accept_with_a_free_text_reason_and_recall_it(self):
        self.two_notes()
        self.cli("draft-proposals", "--project", "p")
        listing = self.cli("review", "list", "--project", "p")
        report = self.waiting()
        revision = report["proposals"][0]["revision"]
        self.assertIn(revision, listing)
        self.assertIn("now retries once per request", listing)
        self.assertIn("repeated_evidence_v1", listing)
        self.assertIn("supported by 2 observation(s)", listing)
        self.assertIn("Scoped the deadline per request in payments/retry.py yesterday.", listing)
        shown = self.cli("review", "show", revision, "--project", "p")
        self.assertIn("applies to: everything in this project", shown)
        # Free text with spaces is a human reason, not an identifier.
        accepted = self.cli(
            "review",
            "accept",
            revision,
            "--project",
            "p",
            "--reason",
            "Matches what we agreed in standup",
        )
        self.assertEqual(json.loads(accepted)["state"], "accepted")
        self.assertEqual(self.waiting()["proposals"], [])
        recalled = self.cli("recall", "--project", "p", "--query", "retries per request")
        self.assertIn("accepted_knowledge", recalled)
        audit = json.loads(self.cli("review", "show", revision, "--project", "p", "--json"))
        self.assertEqual(audit["state"], "accepted")
        self.assertEqual(audit["decision"]["note"], "Matches what we agreed in standup")
        self.assertEqual(audit["decision"]["reason"], "reviewed_in_cli")

    def test_reject_needs_a_reason_and_listing_pages_oldest_first(self):
        for n, topic in enumerate(("alpha/one.py", "beta/two.py", "gamma/three.py")):
            self.two_notes(topic, suffix=str(n))
        self.cli("draft-proposals", "--project", "p")
        everything = json.loads(
            self.cli("review", "list", "--project", "p", "--limit", "50", "--json")
        )["proposals"]
        self.assertGreaterEqual(len(everything), 3)
        first = json.loads(self.cli("review", "list", "--project", "p", "--limit", "2", "--json"))
        self.assertEqual(len(first["proposals"]), 2)
        self.assertTrue(first["more"])
        positions = [p["created_position"] for p in everything]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(first["proposals"], everything[:2])
        rest = json.loads(
            self.cli(
                "review",
                "list",
                "--project",
                "p",
                "--limit",
                "50",
                "--json",
                "--after",
                str(first["next_after"]),
            )
        )
        self.assertEqual(rest["proposals"], everything[2:])
        self.assertFalse(rest["more"])
        victim = first["proposals"][0]["revision"]
        self.cli("review", "reject", victim, "--project", "p", expect=2)
        done = self.cli(
            "review", "reject", victim, "--project", "p", "--reason", "Too vague to act on"
        )
        self.assertEqual(json.loads(done)["state"], "reject")
        self.assertNotIn(victim, [p["revision"] for p in self.waiting()["proposals"]])

    def test_review_needs_the_review_capability_exactly_as_before(self):
        self.two_notes()
        self.cli("draft-proposals", "--project", "p")
        revision = self.waiting()["proposals"][0]["revision"]
        reader = self.credential("reader", ["read"])
        denied = self.cli("review", "list", "--project", "p", credential=reader, expect=2)
        self.assertIn("denied", denied)
        denied = self.cli(
            "review",
            "accept",
            revision,
            "--project",
            "p",
            "--reason",
            "x",
            credential=reader,
            expect=2,
        )
        self.assertIn("denied", denied)
        reviewer = self.credential("reviewer", ["review", "read"])
        self.cli(
            "review",
            "accept",
            revision,
            "--project",
            "p",
            "--reason",
            "Looks right",
            credential=reviewer,
        )

    def test_accepting_a_draft_as_a_constraint_files_and_accepts_one(self):
        self.two_notes("billing/rates.py")
        self.cli("draft-proposals", "--project", "p")
        revision = self.waiting()["proposals"][0]["revision"]
        result = json.loads(
            self.cli(
                "review",
                "accept",
                revision,
                "--project",
                "p",
                "--as",
                "constraint",
                "--effect",
                "prohibit",
                "--reason",
                "Standing rule",
                "--json",
            )
        )
        self.assertIn("constraint", result)
        self.assertEqual(result["draft"]["state"], "reject")
        recalled = self.cli("recall", "--project", "p", "--query", "billing/rates.py")
        self.assertIn("## Must follow", recalled)
        self.assertIn("(prohibit)", recalled)
        self.assertEqual(self.waiting()["proposals"], [])
        listed = json.loads(
            self.cli("review", "list", "--project", "p", "--kind", "constraint", "--json")
        )
        self.assertEqual(listed["proposals"], [])

    def test_retirement_requests_are_listed_and_decided_from_the_terminal(self):
        agent = self.credential("agent", ["capture", "read"])
        reviewer = self.credential("reviewer", ["review", "read"])
        mine = self.capture("mine", "The staging key is abc123.", credential=agent)
        forgot = json.loads(
            self.cli(
                "recall", "--project", "p", "--query", "staging key", "--json", credential=agent
            )
        )
        self.assertTrue(forgot["constraints"] == [])
        client = LocalClient(self.server.path, agent, "p")
        asked = lean.call(client, "forget", {"memory": mine, "why": "It contains a key."})
        self.assertEqual(asked["state"], "open")
        listing = self.cli("review", "list", "--project", "p", credential=reviewer)
        self.assertIn(asked["request"], listing)
        self.assertIn("It contains a key.", listing)
        self.assertIn("The staging key is abc123.", listing)
        # A reviewer without the admin grant cannot approve; an admin can.
        denied = self.cli(
            "review",
            "retire-approve",
            asked["request"],
            "--project",
            "p",
            credential=reviewer,
            expect=2,
        )
        self.assertIn("denied", denied)
        approved = self.cli("review", "retire-approve", asked["request"], "--project", "p")
        self.assertEqual(json.loads(approved)["state"], "approved")
        gone = self.cli("recall", "--project", "p", "--query", "staging key")
        self.assertNotIn("abc123", gone)
        # Dismissal leaves the record in place.
        keep = self.capture("keep", "The deploy window is Tuesday.", credential=agent)
        again = lean.call(client, "forget", {"memory": keep, "why": "Maybe stale."})
        self.cli(
            "review",
            "retire-dismiss",
            again["request"],
            "--project",
            "p",
            "--reason",
            "Checked, still true",
            credential=reviewer,
        )
        self.assertIn(
            "deploy window", self.cli("recall", "--project", "p", "--query", "deploy window")
        )

    def test_capture_supersedes_from_the_cli(self):
        old = self.capture("old", "The cache ttl is five minutes.")
        new = self.capture(
            "new", "The cache ttl is now ten minutes.", "cli", None, "--supersedes", old
        )
        recalled = self.cli("recall", "--project", "p", "--query", "cache ttl")
        self.assertIn("ten minutes", recalled)
        self.assertNotIn("five minutes", recalled)
        self.assertIn(new, recalled)
        self.cli(
            "capture",
            "--project",
            "p",
            "--text",
            "Nothing earlier.",
            "--supersedes",
            "obs_nope",
            expect=2,
        )


class LeanChatReviewTests(DaemonCase):
    def seeded(self, credential=None):
        a, b = self.bridge(
            [
                ("remember", {"text": "The nightly export stalls when disk is full."}),
                ("remember", {"text": "Export stalled again; the disk was full at 02:00."}),
            ],
            credential,
        )
        return self.payload(a)["observation"], self.payload(b)["observation"]

    def test_propose_files_a_proposal_and_confirm_needs_the_operator_switch(self):
        first, second = self.seeded()
        proposed, confirmed = self.bridge(
            [
                (
                    "propose",
                    {
                        "text": "The nightly export stalls when the disk is full.",
                        "evidence": [first, second],
                    },
                ),
                ("confirm", {"proposal": "unused", "decision": "accept", "user_said": "yes"}),
            ]
        )
        filed = self.payload(proposed)
        self.assertEqual(filed["state"], "proposed")
        # Without the operator's switch `confirm` is not in the catalog at all.
        self.assertEqual("unknown_tool", confirmed["error"]["message"])
        # Still only a proposal: not in a briefing as accepted knowledge.
        recalled = self.cli("recall", "--project", "p", "--query", "export stalls disk")
        self.assertNotIn("accepted_knowledge", recalled)
        # Filing twice is the same proposal.
        again = self.payload(
            self.bridge(
                [
                    (
                        "propose",
                        {
                            "text": "The nightly export stalls when the disk is full.",
                            "evidence": [first, second],
                        },
                    )
                ]
            )[0]
        )
        self.assertEqual(again["proposal"], filed["proposal"])
        listing = self.cli("review", "list", "--project", "p")
        self.assertIn(filed["proposal"], listing)
        self.assertIn("agent_proposal_v1", listing)

    def test_confirm_records_the_users_decision_and_names_the_proposer(self):
        first, second = self.seeded()
        proposal = self.payload(
            self.bridge(
                [
                    (
                        "propose",
                        {"text": "Exports stall on a full disk.", "evidence": [first, second]},
                    )
                ]
            )[0]
        )["proposal"]
        (accepted,) = self.bridge(
            [
                (
                    "confirm",
                    {
                        "proposal": proposal,
                        "decision": "accept",
                        "user_said": "Yes, that is right, keep it.",
                    },
                )
            ],
            None,
            "--allow-chat-review",
        )
        self.assertFalse(accepted["isError"], accepted)
        self.assertEqual(self.payload(accepted)["state"], "accept")
        audit = json.loads(self.cli("review", "show", proposal, "--project", "p", "--json"))
        decision = audit["decision"]
        self.assertEqual(decision["confirmation"]["channel"], "chat")
        self.assertEqual(decision["confirmation"]["proposed_by"], "owner")
        self.assertEqual(decision["confirmation"]["relayed_by"], "owner")
        self.assertIn("Yes, that is right, keep it.", decision["note"])
        shown = self.cli("review", "show", proposal, "--project", "p")
        self.assertIn("confirmed in chat", shown)
        recalled = self.cli("recall", "--project", "p", "--query", "exports stall disk")
        self.assertIn("accepted_knowledge", recalled)
        # A reviewer can revert it: deprecate through the ordinary review action.
        client = LocalClient(self.server.path, self.owner_file, "p")
        item = json.loads(self.cli("review", "show", proposal, "--project", "p", "--json"))
        client.knowledge(
            {
                "version": 1,
                "incarnation": client.binding[-1],
                "action": "review",
                "key": "revert-1",
                "args": {
                    "project": "p",
                    "revision": proposal,
                    "expected_version": item["family_version"],
                    "decision": "deprecate",
                    "reason": "reverted_after_audit",
                    "breadth_reviewed": True,
                },
            }
        )
        again = self.cli("recall", "--project", "p", "--query", "exports stall disk")
        self.assertNotIn("accepted_knowledge", again)

    def test_confirm_without_the_review_capability_fails_even_when_enabled(self):
        writer = self.credential("writer", ["capture", "read"])
        first, second = self.seeded(writer)
        proposal = self.payload(
            self.bridge(
                [
                    (
                        "propose",
                        {"text": "Exports stall on a full disk.", "evidence": [first, second]},
                    )
                ],
                writer,
            )[0]
        )["proposal"]
        (denied,) = self.bridge(
            [("confirm", {"proposal": proposal, "decision": "accept", "user_said": "yes"})],
            writer,
            "--allow-chat-review",
        )
        self.assertTrue(denied["isError"])
        self.assertEqual(self.payload(denied)["error"], "denied")
        waiting = json.loads(self.cli("review", "list", "--project", "p", "--json"))
        self.assertEqual([p["revision"] for p in waiting["proposals"]], [proposal])

    def test_confirm_never_touches_constraints_or_decided_proposals(self):
        first, second = self.seeded()
        # A constraint proposal filed outside chat cannot be accepted through chat.
        self.two = (first, second)
        client = LocalClient(self.server.path, self.owner_file, "p")
        span = lean._evidence(client, first)
        filed = lean._knowledge(
            client,
            "propose",
            {
                "logical_key": "rule",
                "expected_version": 0,
                "body": {
                    "text": "Never delete the export directory.",
                    "kind": "constraint",
                    "selector": {"selector_version": "1", "any_of": [{"all_of": [{"op": "any"}]}]},
                    "valid_start": None,
                    "valid_end": None,
                    "task": None,
                    "run": None,
                    "readers": None,
                    "evidence": [span],
                    "authority": "project",
                    "rule_key": "export",
                    "effect": "prohibit",
                    "rule_value": "delete",
                    "provenance": {},
                },
            },
            "rule-key",
        )
        (refused,) = self.bridge(
            [("confirm", {"proposal": filed["revision"], "decision": "accept", "user_said": "ok"})],
            None,
            "--allow-chat-review",
        )
        self.assertTrue(refused["isError"])
        self.assertEqual(self.payload(refused)["error"], "unavailable")
        # The server refuses a chat decision on a constraint even if asked directly.
        item = json.loads(self.cli("review", "show", filed["revision"], "--project", "p", "--json"))
        with self.assertRaises(Exception) as caught:
            lean._knowledge(
                client,
                "review",
                {
                    "revision": filed["revision"],
                    "expected_version": item["family_version"],
                    "decision": "accept",
                    "reason": "confirmed_in_chat",
                    "channel": "chat",
                    "breadth_reviewed": True,
                },
                "direct",
            )
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_correct_replaces_and_forget_files_a_request(self):
        (remembered,) = self.bridge([("remember", {"text": "The deploy window is Monday."})])
        old = self.payload(remembered)["observation"]
        corrected, recalled = self.bridge(
            [
                ("correct", {"memory": old, "text": "The deploy window is now Tuesday."}),
                ("recall", {"query": "deploy window"}),
            ]
        )
        self.assertFalse(corrected["isError"], corrected)
        text = self.payload(recalled)["text"]
        self.assertIn("now Tuesday", text)
        self.assertNotIn("is Monday", text)
        new = self.payload(corrected)["observation"]
        asked, after = self.bridge(
            [
                ("forget", {"memory": new, "why": "Not true any more."}),
                ("recall", {"query": "deploy window"}),
            ]
        )
        self.assertEqual(self.payload(asked)["state"], "open")
        # Hidden from the requester's own briefing; the superseded record returns, as the
        # replacement is no longer visible to this reader.
        self.assertNotIn("now Tuesday", self.payload(after)["text"])
        # A principal that cannot read the record cannot ask about it.
        stranger = self.credential("stranger", ["capture", "read"], project="q")
        with self.assertRaises(LocalError):
            LocalClient(self.server.path, stranger, "p")
        outsider = self.credential("outsider", ["capture", "read"])
        private = self.payload(
            self.bridge(
                [
                    (
                        "remember",
                        {"text": "Private.", "readers": ["owner"], "classification": "restricted"},
                    )
                ]
            )[0]
        )["observation"]
        (refused,) = self.bridge([("forget", {"memory": private, "why": "x"})], outsider)
        self.assertTrue(refused["isError"])
        self.assertEqual(self.payload(refused)["error"], "unavailable")


class SessionStartTests(DaemonCase):
    def test_fifty_sessions_later_the_start_still_carries_the_standing_instruction(self):
        client = LocalClient(self.server.path, self.owner_file, "p")
        instruction = "Never mock the database in integration tests; use the real one."
        for session in range(1, 51):
            self.capture(
                f"hook-{session}",
                f"Session {session} transcript tail: edited modules, ran pytest, noted nothing "
                f"durable about topic{session}.",
                source="claude-session",
            )
            if session == 7:
                self.capture("standing", instruction, source="user-instruction")
            if session % 10 == 0:
                lean.call(
                    client,
                    "save_progress",
                    {
                        "task": "billing",
                        "objective": "Ship the billing fix",
                        "current_state": f"After session {session}",
                        "next_actions": [f"Backfill the invoices table (after session {session})"],
                        "failed_attempts": [
                            {"action": "Ran the migration twice", "outcome": "Deadlock"}
                        ],
                    },
                )
        out = subprocess.run(
            [
                sys.executable,
                "-m",
                "memoryking",
                "hook",
                "session-start",
                "--project",
                "p",
                "--task",
                "billing",
                "--socket",
                str(self.server.path),
                "--credential",
                str(self.owner_file),
            ],
            input="{}",
            capture_output=True,
            text=True,
            env=self.env,
            timeout=60,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(instruction, out.stdout)
        self.assertIn("Backfill the invoices table (after session 50)", out.stdout)
        self.assertNotIn("after session 40)", out.stdout)
        self.assertIn("Do not repeat", out.stdout)
        self.assertIn("Ran the migration twice", out.stdout)
        # The same command line with --query keeps the old, query-driven briefing.
        queried = subprocess.run(
            [
                sys.executable,
                "-m",
                "memoryking",
                "hook",
                "session-start",
                "--project",
                "p",
                "--query",
                "topic12",
                "--socket",
                str(self.server.path),
                "--credential",
                str(self.owner_file),
            ],
            input="{}",
            capture_output=True,
            text=True,
            env=self.env,
            timeout=60,
        )
        self.assertIn("topic12", queried.stdout)

    def test_recall_start_from_the_cli_and_pins(self):
        empty = self.cli("recall", "--project", "p", "--start")
        self.assertIn("Nothing is recorded for this project yet", empty)
        note = self.capture("n", "Releases are cut from the main branch only.")
        self.capture("agent", "Ran tests.", source="agent")
        self.assertNotIn("Releases are cut", self.cli("recall", "--project", "p", "--start"))
        pinned = json.loads(self.cli("pin", "add", "--project", "p", "--observation", note))
        self.assertEqual(pinned["mode"], "add")
        self.assertIn("Pinned", self.cli("recall", "--project", "p", "--start"))
        self.assertIn(note, self.cli("pin", "list", "--project", "p"))
        denied = self.cli(
            "pin",
            "add",
            "--project",
            "p",
            "--observation",
            note,
            credential=self.credential("agent", ["capture", "read"]),
            expect=2,
        )
        self.assertIn("denied", denied)
        self.cli("pin", "remove", "--project", "p", "--observation", note)
        self.assertNotIn("Releases are cut", self.cli("recall", "--project", "p", "--start"))
        self.cli("recall", "--project", "p", expect=2)
        self.cli("recall", "--project", "p", "--start", "--query", "x", expect=2)
        as_json = json.loads(self.cli("recall", "--project", "p", "--start", "--json"))
        self.assertEqual(as_json["status"], "no_relevant_memory")

    def test_lean_recall_still_requires_a_query(self):
        recall = next(t for t in lean.TOOLS if t["name"] == "recall")
        self.assertEqual(recall["inputSchema"]["required"], ["query"])
        for tool in lean.TOOLS:
            self.assertNotIn("oneOf", json.dumps(tool["inputSchema"]))


if __name__ == "__main__":
    unittest.main()
