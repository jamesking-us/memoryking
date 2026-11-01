"""Team activity and audit export are bounded, authorized reads over recorded facts."""

import contextlib
import json
import unittest

import test_knowledge as knowledge
import test_retrieval as retrieval
import test_working as working

from memoryking.adapters import team


class TeamCases:
    """Shared by the SQLite fixture and the native PostgreSQL fixture."""

    def gate(self):
        """SQLite reads need no open transaction; the PostgreSQL fixture opens one."""
        return contextlib.nullcontext()

    def who(self, credential=None):
        with self.gate():
            return self.store._auth(
                credential or self.owner, "read", "p", self.store.clock.utc_ms()
            )

    def activity(self, who, project, at, **kw):
        with self.gate():
            return team.activity(self.store, who, project, at, **kw)

    def export(self, who, project, at):
        with self.gate():
            return team.export(self.store, who, project, at)

    def test_activity_counts_each_principals_records_with_their_grants(self):
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        claim = self.propose()
        self.review(claim)
        saved = self.save()
        self.run_r()  # a briefing by the owner
        at = self.store.clock.utc_ms()
        result = self.activity(self.who(), "p", at)
        self.assertEqual(result["project"], "p")
        self.assertTrue(result["complete"])
        self.assertEqual(result["days"], 30)
        by = {p["principal"]: p for p in result["principals"]}
        owner = by["owner"]
        self.assertGreaterEqual(owner["events"]["captures"], 1)
        self.assertGreaterEqual(owner["events"]["claims"], 2)  # propose and review
        self.assertEqual(owner["events"]["checkpoints"], 1)
        self.assertEqual(owner["briefings"], 1)
        self.assertEqual(owner["dispatches_delivered"], 0)
        self.assertIn("admin", owner["grants"])
        self.assertIsNotNone(owner["last_seen_ms"])
        self.assertEqual(by["reader"]["grants"], ["read"])
        self.assertEqual(by["reader"]["events"], {})
        self.assertIsNone(by["reader"]["last_seen_ms"])
        self.assertEqual([p["principal"] for p in result["principals"]][0], "owner")
        # Another project's records are not counted; the window is bounded.
        elsewhere = self.activity(self.who(), "other", at)["principals"]
        self.assertEqual([p["principal"] for p in elsewhere], ["owner"])
        self.assertIn("admin", elsewhere[0]["grants"])
        self.assertEqual(elsewhere[0]["events"], {})
        self.assertEqual(self.activity(self.who(), "p", at, days=1000)["days"], 365)
        self.assertNotIn(saved["checkpoint"], json.dumps(result))
        self.assertNotIn("timeout=30", json.dumps(result))
        self.assertEqual(len(self.activity(self.who(reader), "p", at)["principals"]), 2)

    def test_export_is_content_free_bounded_and_names_what_it_holds(self):
        claim = self.propose()
        self.review(claim)
        saved = self.save()
        briefing = self.run_r()
        self.store.purge(self.owner, self.obs["observation"])
        at = self.store.clock.utc_ms()
        who = self.store._auth(self.owner, "admin", "*", at)
        result = self.export(who, "p", at)
        self.assertEqual(result["schema"], "audit-export-v1")
        self.assertEqual(result["generated_by"], "owner")
        kinds = {e["kind"] for e in result["events"]}
        self.assertIn("purge", kinds)
        self.assertIn("checkpoint_saved", kinds)
        self.assertEqual(result["egress"], [])
        receipts = {a["receipt"]: a for a in result["artifacts"]}
        self.assertIn(briefing["receipt"], receipts)
        self.assertEqual(receipts[briefing["receipt"]]["state"], "purged")
        self.assertEqual(result["truncated"], [])
        self.assertEqual(result["grants"][0]["principal"], "owner")
        text = json.dumps(result)
        self.assertNotIn("timeout=30", text)
        self.assertNotIn(saved["body_hash"][:8], text.replace(saved["body_hash"], ""))
        for key in ("bounds", "content", "incarnation", "store"):
            self.assertIn(key, result)


class TeamTests(TeamCases, unittest.TestCase):
    setUp = working.WorkingTests.setUp
    tearDown = working.WorkingTests.tearDown
    propose = knowledge.KnowledgeTests.propose
    review = knowledge.KnowledgeTests.review
    body = knowledge.KnowledgeTests.body
    run_k = knowledge.KnowledgeTests.run_k
    save = working.WorkingTests.save
    scope = working.WorkingTests.scope
    command = working.WorkingTests.command
    execute = working.WorkingTests.execute
    run_r = working.WorkingTests.run_r
    brief_args = retrieval.RetrievalTests.brief_args


if __name__ == "__main__":
    unittest.main()
