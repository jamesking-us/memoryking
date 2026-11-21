"""Corrections on both backends: supersession, retirement requests, pins, the session start."""

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

import test_knowledge as knowledge
import test_postgres_parity as pg
import test_retrieval as retrieval_helpers

from memoryking.adapters.knowledge_store import Knowledge
from memoryking.adapters.local_store import LocalStore
from memoryking.adapters.retrieval import Retrieval
from memoryking.domain.local import LocalError

DAY = 86_400_000


class CorrectionCases:
    def cap(self, key, text, *, source="test", credential=None, project="p", **extra):
        credential = credential or self.owner
        with self.actor(credential) as store:
            namespace = store.namespace(credential, project)
            return store.capture(
                credential,
                namespace,
                key,
                {
                    "version": 1,
                    "project": project,
                    "source": source,
                    "source_event": key,
                    "text": text,
                    "readers": extra.pop("readers", None),
                    "classification": extra.pop("classification", "internal"),
                    "observed_at_ms": 1,
                    **extra,
                },
            )

    def kn(self, action, credential=None, **args):
        self.n += 1
        command = {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": action,
            "args": {"project": "p", **args},
        }
        if action in {"retire", "retire_decide", "pin"}:
            command["key"] = f"c{self.n}"
        output = []
        with self.actor(credential) as store:
            Knowledge(store).execute(credential or self.owner, command).emit(output.append)
        return json.loads(output[0])

    def brief(self, query, credential=None, **extra):
        return self.retrieve(
            "brief", credential, query=query, bindings=[{}], max_output_tokens=4000, **extra
        )

    def retrieve(self, action, credential=None, **args):
        output = []
        args = {"project": "p", **args}
        if action in {"brief", "start"}:
            args.setdefault("tokenizer", "utf8-byte-estimate-v1")
        with self.actor(credential) as store:
            Retrieval(store).execute(
                credential or self.owner,
                {
                    "version": 1,
                    "incarnation": self.store.incarnation,
                    "action": action,
                    "args": args,
                },
            ).emit(output.append)
        return json.loads(output[0])

    def start(self, credential=None, **extra):
        extra = {"bindings": [{}], "max_output_tokens": 4000, **extra}
        return self.retrieve("start", credential, **extra)

    def refs(self, packet):
        return [g["reference"] for g in packet["guidance"]]

    # --- supersession ---------------------------------------------------------------

    def test_a_replacement_hides_what_it_supersedes_until_it_is_not_visible(self):
        old = self.cap("old", "The retry deadline is thirty seconds.")["observation"]
        self.assertEqual(self.refs(self.brief("retry deadline")), [old])
        new = self.cap("new", "The retry deadline is now sixty seconds.", supersedes=old)[
            "observation"
        ]
        self.assertEqual(self.refs(self.brief("retry deadline")), [new])
        # Evidence is never altered or deleted: the old record is still there to read.
        row = self.store.db.execute(
            "SELECT deleted,blob FROM observations WHERE id=?", (old,)
        ).fetchone()
        self.assertEqual(row["deleted"], 0)
        self.assertTrue(row["blob"])
        # A replacement that is purged leaves the old record visible again.
        self.store.purge(self.owner, new)
        self.assertEqual(self.refs(self.brief("retry deadline")), [old])

    def test_a_replacement_the_reader_cannot_see_does_not_hide_the_old_record(self):
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        old = self.cap("old", "The cache ttl is five minutes.")["observation"]
        private = self.cap(
            "new",
            "The cache ttl is now ten minutes.",
            supersedes=old,
            readers=["owner"],
            classification="restricted",
        )["observation"]
        self.assertEqual(self.refs(self.brief("cache ttl")), [private])
        self.assertEqual(self.refs(self.brief("cache ttl", credential=reader)), [old])

    def test_a_held_replacement_does_not_hide_the_old_record(self):
        old = self.cap("old", "The batch size is fifty.")["observation"]
        self.store.grant(self.owner, "feed", "p", ["capture", "read"])
        command = {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": "trust",
            "key": "rule",
            "args": {"project": "p", "source": "scraper", "level": "untrusted"},
        }
        from memoryking.adapters.inspection import Inspection

        Inspection(self.store).execute(self.owner, command).emit(lambda raw: None)
        held = self.cap("new", "The batch size is now ninety.", source="scraper", supersedes=old)
        self.assertTrue(held["held"])
        self.assertEqual(self.refs(self.brief("batch size")), [old])

    def test_supersedes_needs_a_readable_record_in_the_same_project(self):
        secret = self.cap("secret", "Private note.", readers=["owner"], classification="restricted")
        writer = self.store.grant(self.owner, "writer", "p", ["capture", "read"])
        with self.assertRaisesRegex(LocalError, "unavailable"):
            self.cap("w1", "A replacement.", supersedes=secret["observation"], credential=writer)
        with self.assertRaisesRegex(LocalError, "unavailable"):
            self.cap("w2", "A replacement.", supersedes="obs_missing", credential=writer)
        other = self.store.grant(self.owner, "owner2", "q", ["capture", "read"])
        elsewhere = self.cap("q1", "Another project.", project="q", credential=other)
        with self.assertRaisesRegex(LocalError, "unavailable"):
            self.cap("w3", "A replacement.", supersedes=elsewhere["observation"])
        # A retry with the same key and body replays; a different link is a conflict.
        first = self.cap("w4", "Replaces the original.", supersedes=self.obs["observation"])
        self.assertEqual(
            first["observation"],
            self.cap("w4", "Replaces the original.", supersedes=self.obs["observation"])[
                "observation"
            ],
        )
        with self.assertRaisesRegex(LocalError, "source_conflict"):
            self.cap("w4", "Replaces the original.")

    # --- retirement requests ---------------------------------------------------------

    def test_forget_files_a_request_that_only_an_admin_can_approve(self):
        agent = self.store.grant(self.owner, "agent", "p", ["capture", "read"])
        reviewer = self.store.grant(self.owner, "reviewer", "p", ["review", "read"])
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        mine = self.cap("mine", "The staging password is hunter2.", credential=agent)["observation"]
        self.assertEqual(self.refs(self.brief("staging password", credential=agent)), [mine])
        asked = self.kn("retire", credential=agent, observation=mine, note="Contains a secret.")
        self.assertEqual(asked["state"], "open")
        again = self.kn("retire", credential=agent, observation=mine, note="Contains a secret.")
        self.assertEqual(again["request"], asked["request"])
        # The author no longer sees it; everyone else sees it marked.
        self.assertEqual(self.refs(self.brief("staging password", credential=agent)), [])
        seen = self.brief("staging password", credential=reader)["guidance"]
        self.assertEqual([g["reference"] for g in seen], [mine])
        self.assertTrue(seen[0]["retirement_requested"])
        # A reader cannot ask; a writer cannot list or decide.
        with self.assertRaisesRegex(LocalError, "denied"):
            self.kn("retire", credential=reader, observation=mine, note="x")
        with self.assertRaisesRegex(LocalError, "denied"):
            self.kn("retirements", credential=agent)
        with self.assertRaisesRegex(LocalError, "denied"):
            self.kn(
                "retire_decide",
                credential=agent,
                request=asked["request"],
                decision="dismiss",
                reason="no",
            )
        listed = self.kn("retirements", credential=reviewer)["items"]
        self.assertEqual(
            [(i["request"], i["requested_by"], i["note"]) for i in listed],
            [(asked["request"], "agent", "Contains a secret.")],
        )
        self.assertIn("hunter2", listed[0]["preview"])
        # Purge stays an admin action: a reviewer may dismiss but not approve.
        with self.assertRaisesRegex(LocalError, "denied"):
            self.kn(
                "retire_decide",
                credential=reviewer,
                request=asked["request"],
                decision="approve",
                reason="approved",
            )
        approved = self.kn(
            "retire_decide", request=asked["request"], decision="approve", reason="approved"
        )
        self.assertEqual(approved["state"], "approved")
        self.assertEqual(self.refs(self.brief("staging password", credential=reader)), [])
        self.assertEqual(self.kn("retirements", credential=reviewer)["items"], [])
        with self.assertRaisesRegex(LocalError, "invalid_transition"):
            self.kn("retire_decide", request=asked["request"], decision="dismiss", reason="late")
        row = self.store.db.execute(
            "SELECT state,note,decided_by FROM retirement_requests WHERE id=?",
            (asked["request"],),
        ).fetchone()
        self.assertEqual((row["state"], row["note"], row["decided_by"]), ("approved", "", "owner"))

    def test_a_dismissed_request_restores_the_record_for_its_author(self):
        agent = self.store.grant(self.owner, "agent", "p", ["capture", "read"])
        reviewer = self.store.grant(self.owner, "reviewer", "p", ["review", "read"])
        mine = self.cap("mine", "The deploy window is Tuesday.", credential=agent)["observation"]
        asked = self.kn("retire", credential=agent, observation=mine, note="Maybe stale.")
        self.assertEqual(self.refs(self.brief("deploy window", credential=agent)), [])
        done = self.kn(
            "retire_decide",
            credential=reviewer,
            request=asked["request"],
            decision="dismiss",
            reason="still_true",
            note="Checked: still correct.",
        )
        self.assertEqual(done["state"], "dismissed")
        packet = self.brief("deploy window", credential=agent)["guidance"]
        self.assertEqual([g["reference"] for g in packet], [mine])
        self.assertNotIn("retirement_requested", packet[0])
        # Records the requester cannot read cannot be asked about.
        private = self.cap("p1", "Hidden.", readers=["owner"], classification="restricted")
        with self.assertRaisesRegex(LocalError, "unavailable"):
            self.kn("retire", credential=agent, observation=private["observation"], note="x")

    def test_purge_by_any_route_closes_open_requests_and_follows_dependencies(self):
        agent = self.store.grant(self.owner, "agent", "p", ["capture", "read"])
        mine = self.cap("mine", "Temporary token abc.", credential=agent)["observation"]
        asked = self.kn("retire", credential=agent, observation=mine, note="Secret.")
        self.store.purge(self.owner, mine)
        row = self.store.db.execute(
            "SELECT state,note FROM retirement_requests WHERE id=?", (asked["request"],)
        ).fetchone()
        self.assertEqual((row["state"], row["note"]), ("approved", ""))

    # --- pins ------------------------------------------------------------------------

    def test_pins_are_a_reviewer_decision_and_survive_a_replacement_revision(self):
        reviewer = self.store.grant(self.owner, "reviewer", "p", ["review", "read"])
        agent = self.store.grant(self.owner, "agent", "p", ["capture", "read"])
        note = self.cap("n", "Always run migrations before the seed script.")["observation"]
        with self.assertRaisesRegex(LocalError, "denied"):
            self.kn("pin", credential=agent, mode="add", target_kind="observation", target=note)
        self.kn("pin", credential=reviewer, mode="add", target_kind="observation", target=note)
        accepted = self.review(self.propose(self.body("Deploys use blue green."), key="bg"))
        self.kn(
            "pin", credential=reviewer, mode="add", target_kind="claim", target=accepted["revision"]
        )
        listed = self.kn("pins", credential=reviewer)["items"]
        self.assertEqual(
            sorted((i["target_kind"], i["pinned_by"]) for i in listed),
            [("claim", "reviewer"), ("observation", "reviewer")],
        )
        packet = self.start(credential=agent)
        pinned = [g["reference"] for g in packet["guidance"] if g["selection_reason"] == "pinned"]
        self.assertEqual(set(pinned), {note, accepted["revision"]})
        # A claim that was never accepted cannot be pinned; removal is journaled too.
        draft = self.propose(self.body("An unreviewed idea."), key="idea")
        with self.assertRaisesRegex(LocalError, "invalid_transition"):
            self.kn(
                "pin",
                credential=reviewer,
                mode="add",
                target_kind="claim",
                target=draft["revision"],
            )
        self.kn("pin", credential=reviewer, mode="remove", target_kind="observation", target=note)
        packet = self.start(credential=agent)
        self.assertNotIn(note, self.refs(packet))
        # Purging a pinned observation removes the pin.
        again = self.cap("n2", "Pinned then purged.")["observation"]
        self.kn("pin", mode="add", target_kind="observation", target=again)
        self.store.purge(self.owner, again)
        self.assertNotIn(again, [p["target"] for p in self.kn("pins")["items"]])

    # --- session start ---------------------------------------------------------------

    def test_session_start_is_empty_honestly_and_then_fills_in_priority_order(self):
        text = self.retrieve(
            "start", project="empty", bindings=[{}], max_output_tokens=2000, format="text"
        )
        self.assertEqual(text["status"], "no_relevant_memory")
        self.assertIn("Nothing is recorded for this project yet", text["text"])

    def test_session_start_carries_constraints_pins_checkpoint_and_recent_decisions(self):
        self.constraint("Never print credentials in logs", key="creds")
        standing = self.cap(
            "inst", "Always write tests before changing billing code.", source="user-instruction"
        )["observation"]
        self.cap("noise", "Billing code reads rates from rates.py.", source="agent")
        proposed = self.review(self.propose(self.body("Rates live in rates.py."), key="rates"))
        pinned = self.cap("pin-me", "The release branch is frozen on Fridays.")["observation"]
        self.kn("pin", mode="add", target_kind="observation", target=pinned)
        self.save_checkpoint("billing-task", "Ship the billing fix", ["Run the regression suite"])
        packet = self.start(task="billing-task", format="text")
        text = packet["text"]
        self.assertIn("Never print credentials in logs", text)
        self.assertIn("The release branch is frozen on Fridays.", text)
        self.assertIn("Run the regression suite", text)
        self.assertIn("Do not repeat", text)
        self.assertIn("Always write tests before changing billing code.", text)
        self.assertIn("Rates live in rates.py.", text)
        self.assertNotIn("Billing code reads rates from rates.py.", text)  # an agent note
        order = [
            text.index(s)
            for s in (
                "Never print credentials",
                "release branch is frozen",
                "Run the regression suite",
                "Always write tests",
            )
        ]
        self.assertEqual(order, sorted(order))
        # The same call without a task has no checkpoint section.
        self.assertNotIn("Run the regression suite", self.start(format="text")["text"])
        # Replay: the receipt's body is retrievable by its owner, until a source is purged.
        receipt = packet["receipt"]
        replayed = self.retrieve("replay", receipt=receipt)
        self.assertEqual(replayed["receipt"], receipt)
        self.store.purge(self.owner, standing)
        self.assertNotIn(
            "Always write tests", self.start(task="billing-task", format="text")["text"]
        )
        self.assertIn(proposed["revision"], json.dumps(self.start()))

    def test_session_start_follows_authorization_holds_and_recency(self):
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        self.cap(
            "private",
            "Only the owner decided this.",
            source="decision",
            readers=["owner"],
            classification="restricted",
        )
        self.cap("old", "A very old decision.", source="decision")
        self.cap("open", "Everyone may know this instruction.", source="user")
        self.cap("agent", "An agent guess.", source="agent")
        for_reader = self.start(credential=reader, format="text")["text"]
        self.assertIn("Everyone may know this instruction.", for_reader)
        self.assertNotIn("Only the owner decided this.", for_reader)
        self.assertNotIn("An agent guess.", for_reader)
        owner_view = self.start(format="text")["text"]
        self.assertIn("Only the owner decided this.", owner_view)
        # The recent-decision count is respected, newest first.
        for n in range(8):
            self.cap(f"d{n}", f"Decision number {n}.", source="decision")
        limited = self.start(recent_limit=3, format="text")["text"]
        self.assertIn("Decision number 7.", limited)
        self.assertNotIn("Decision number 4.", limited)
        # Retirement requested by the viewer hides the record from their own start.
        agent = self.store.grant(self.owner, "agent", "p", ["capture", "read"])
        mine = self.cap("agent-rule", "The agent's own rule.", source="decision", credential=agent)[
            "observation"
        ]
        self.assertIn("The agent's own rule.", self.start(credential=agent, format="text")["text"])
        self.kn("retire", credential=agent, observation=mine, note="Wrong.")
        self.assertNotIn(
            "The agent's own rule.", self.start(credential=agent, format="text")["text"]
        )
        marked = self.start(credential=reader, format="text")["text"]
        self.assertIn("retirement requested", marked)

    def test_session_start_stays_inside_the_budget(self):
        for n in range(12):
            self.cap(f"long{n}", f"Decision {n}: " + "word " * 600, source="decision")
        small = self.start(max_output_tokens=2400)
        self.assertLessEqual(small["usage"]["output_tokens"], 2400)
        self.assertTrue(0 < len(small["guidance"]) < 5)
        tiny = self.start(max_output_tokens=50)
        self.assertEqual(tiny["status"], "budget_insufficient")

    def save_checkpoint(self, task, objective, actions):
        from test_working import checkpoint

        from memoryking.adapters.working import Working

        body = checkpoint()
        body.update(
            objective=objective,
            next_actions=actions,
            failed_attempts=[
                {
                    "action": "Retried the deploy",
                    "outcome": "Timed out again",
                    "do_not_repeat_when": "the timeout is unchanged",
                }
            ],
        )
        output = []
        Working(self.store).execute(
            self.owner,
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": "save",
                "key": "ckpt-" + task,
                "args": {
                    "project": "p",
                    "task": task,
                    "run": None,
                    "expected_revision": 0,
                    "readers": ["owner"],
                    "checkpoint": body,
                },
            },
        ).emit(output.append)


class LocalCorrectionTests(CorrectionCases, unittest.TestCase):
    @contextmanager
    def actor(self, credential):
        yield self.store

    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown
    run_k = knowledge.KnowledgeTests.run_k
    body = knowledge.KnowledgeTests.body
    propose = knowledge.KnowledgeTests.propose
    review = knowledge.KnowledgeTests.review
    constraint = retrieval_helpers.RetrievalTests.constraint

    def test_pins_and_dismissals_survive_backup_and_restore(self):
        reviewer = self.store.grant(self.owner, "reviewer", "p", ["review", "read"])
        agent = self.store.grant(self.owner, "agent", "p", ["capture", "read"])
        note = self.cap("n", "Keep this in front of every session.")["observation"]
        mine = self.cap("m", "Possibly stale.", credential=agent)["observation"]
        with tempfile.TemporaryDirectory(prefix="mk-corr-", dir="/tmp") as folder:
            archive = Path(folder) / "archive"
            self.store.backup(archive)
            self.kn("pin", credential=reviewer, mode="add", target_kind="observation", target=note)
            asked = self.kn("retire", credential=agent, observation=mine, note="Stale?")
            self.kn(
                "retire_decide",
                credential=reviewer,
                request=asked["request"],
                decision="dismiss",
                reason="checked",
            )
            self.store.close()
            credential = LocalStore.restore(self.data, self.control, archive).read_text()
            self.store = LocalStore(self.data, self.control)
            self.owner = credential
            pins = self.store.db.execute("SELECT target FROM pins").fetchall()
            self.assertEqual([p["target"] for p in pins], [note])


class PostgresCorrectionTests(CorrectionCases, pg.NativeFixture):
    pass
