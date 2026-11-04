"""Proposal drafts from repeated evidence, on both backends: filed, replay-safe, reviewable."""

import json
import unittest

import test_knowledge as knowledge
import test_postgres_parity as pg

from memoryking.adapters.knowledge_store import Knowledge
from memoryking.adapters.proposals import _sentence_with
from memoryking.domain.local import LocalError


class SentenceTests(unittest.TestCase):
    def test_first_sentence_with_the_topic_and_its_byte_offsets(self):
        text = "Intro line. The fix is in payments/retry.py now. Trailing note."
        start, end, sentence = _sentence_with(text, "payments/retry.py")
        self.assertEqual(sentence, "The fix is in payments/retry.py now.")
        self.assertEqual(text.encode()[start:end].decode(), sentence)
        self.assertIsNone(_sentence_with(text, "absent"))
        start, end, sentence = _sentence_with(
            "Zébra café retry_deadline_ms set.", "retry_deadline_ms"
        )
        self.assertEqual("Zébra café retry_deadline_ms set.".encode()[start:end].decode(), sentence)


class ProposalCases:
    def capture(self, key, text):
        return self.store.capture(
            self.owner,
            self.ns,
            key,
            {
                "version": 1,
                "project": "p",
                "source": "test",
                "source_event": key,
                "text": text,
                "readers": None,
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )["observation"]

    def draft(self, key="draft-1", limit=16, credential=None):
        output = []
        Knowledge(self.store).execute(
            credential or self.owner,
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": "draft",
                "key": key,
                "args": {"project": "p", "limit": limit},
            },
        ).emit(output.append)
        return json.loads(output[0])

    def test_repeated_anchor_yields_one_reviewable_draft_and_replays(self):
        a = self.capture("a", "Scoped the deadline per request in payments/retry.py yesterday.")
        b = self.capture("b", "Review note: payments/retry.py now retries once per request.")
        self.capture("c", "Lunch was good.")
        first = self.draft()
        self.assertEqual(len(first["drafted"]), 1)
        draft = first["drafted"][0]
        self.assertEqual(
            (draft["topic"], draft["basis"], draft["support"]), ("payments/retry.py", "path", 2)
        )
        self.assertEqual(
            draft["text"], "Review note: payments/retry.py now retries once per request."
        )
        again = self.draft()  # same key: replayed, not refiled
        self.assertEqual(again["drafted"][0]["revision"], draft["revision"])
        fresh = self.draft(key="draft-2")  # new key, same evidence: nothing new
        self.assertEqual(fresh["drafted"], [])
        self.assertEqual(fresh["skipped"]["already_proposed"], 1)
        # The draft is an ordinary proposal: proposed, cited, reviewable, never self-accepted.
        listed = self.run_k("query", {"project": "p", "bindings": [{}], "diagnostic": True})
        items = [i for i in listed["items"] if i["revision"] == draft["revision"]]
        self.assertEqual(items[0]["state"], "proposed")
        self.assertEqual({s["observation"] for s in items[0]["body"]["evidence"]}, {a, b})
        self.assertEqual(items[0]["body"]["provenance"]["method"], "repeated_evidence_v1")
        accepted = self.review({"revision": draft["revision"], "family_version": 1})
        self.assertEqual(accepted["state"], "accept")
        # New evidence on the same topic drafts a new revision on the same family.
        self.capture("d", "Third note: payments/retry.py is stable after the change.")
        more = self.draft(key="draft-3")
        same_family = [d for d in more["drafted"] if d["family"] == draft["family"]]
        self.assertEqual(len(same_family), 1)
        self.assertEqual(same_family[0]["support"], 3)
        self.assertNotEqual(same_family[0]["revision"], draft["revision"])

    def test_single_source_held_and_lone_observations_draft_nothing(self):
        self.capture("x", "A lone remark about the garden gate.")
        self.assertEqual(self.draft()["drafted"], [])
        # Two captures of the same event are one occurrence, not repetition.
        self.capture("same", "Same event: build on jenkins.internal failed.")
        self.store.capture(
            self.owner,
            self.ns,
            "same-again",
            {
                "version": 1,
                "project": "p",
                "source": "other",
                "source_event": "same",
                "text": "Same event: build on jenkins.internal failed.",
                "readers": None,
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )
        result = self.draft(key="draft-2")
        self.assertEqual([d["topic"] for d in result["drafted"]], [])
        self.assertGreaterEqual(result["skipped"]["single_source"], 1)
        # A reader cannot draft; drafting needs the capture capability.
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        with self.assertRaisesRegex(LocalError, "denied"):
            self.draft(key="draft-3", credential=reader)

    def test_shared_rare_term_drafts_when_no_anchor_exists_and_the_cap_holds(self):
        self.capture("t1", "The nightly export stalls when the tablespace fills.")
        self.capture("t2", "Again the tablespace filled during the export window.")
        self.capture("u1", "The warehouse loader doubled its batch size.")
        self.capture("u2", "Batch size for the warehouse loader is now doubled.")
        result = self.draft(key="terms", limit=1)
        self.assertEqual(len(result["drafted"]), 1)
        self.assertEqual(result["drafted"][0]["basis"], "term")
        self.assertEqual(result["drafted"][0]["support"], 2)
        self.assertGreaterEqual(result["skipped"].get("limit", 0), 1)
        rest = self.draft(key="terms-2", limit=16)
        self.assertEqual(len(rest["drafted"]), 1)
        self.assertEqual(rest["skipped"]["already_proposed"], 1)


class LocalProposalTests(ProposalCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown
    run_k = knowledge.KnowledgeTests.run_k
    review = knowledge.KnowledgeTests.review


class PostgresProposalTests(ProposalCases, pg.NativeFixture):
    run_k = knowledge.KnowledgeTests.run_k
    review = knowledge.KnowledgeTests.review
