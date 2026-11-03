"""The memory-health report on both backends: counts the store already keeps, no content."""

import hashlib
import json
import unittest

import test_experience as experience
import test_inspection as inspection
import test_knowledge as knowledge
import test_postgres_parity as pg

from memoryking.adapters.inspection import Inspection
from memoryking.domain.local import LocalError


class HealthCases:
    body = knowledge.KnowledgeTests.body
    propose = knowledge.KnowledgeTests.propose
    review = knowledge.KnowledgeTests.review
    run_k = knowledge.KnowledgeTests.run_k
    run_e = experience.ProcedureCases.run_e
    run_r = experience.ProcedureCases.run_r
    brief_args = experience.ProcedureCases.brief_args
    inspect = inspection.InspectionCases.inspect
    session = inspection.InspectionCases.session
    browser = inspection.InspectionCases.browser

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

    def feedback(self, brief, reference, version=0, **changes):
        return self.run_e(
            "feedback",
            {
                "project": "p",
                "receipt": brief["receipt"],
                "reference": reference,
                "task": "task",
                "run": "run",
                "expected_revision": version,
                "use": "unknown",
                "usefulness": "unknown",
                "comment": None,
                "readers": None,
                **changes,
            },
        )

    def levels(self, report):
        return {s["name"]: s["level"] for s in report["signals"]}

    def test_quiet_project_reports_unknowns_and_an_empty_backlog(self):
        report = self.inspect("health")
        self.assertEqual(report["window"]["days"], 30)
        self.assertEqual(report["observations"]["total"], 1)
        self.assertEqual(report["observations"]["recent"], 1)
        self.assertEqual(report["observations"]["never_delivered"], 1)
        self.assertEqual(report["briefings"]["total"], 0)
        self.assertIsNone(report["briefings"]["no_relevant_memory_ppm"])
        self.assertEqual(report["feedback"]["votes"], 0)
        self.assertIsNone(report["feedback"]["helpful_ppm"])
        self.assertEqual(report["claims"]["families"], 0)
        self.assertEqual(
            report["backlog"],
            {"proposed": 0, "oldest_age_ms": None, "items": [], "truncated": False},
        )
        self.assertEqual(
            self.levels(report),
            {
                "feedback": "unknown",
                "briefings": "unknown",
                "backlog": "ok",
                "evidence": "ok",
                "reach": "unknown",
            },
        )
        self.assertIn("counts_only_no_text_no_principals", report["coverage"])
        # The window is bounded and the report is admin-only; the browser may ask for it.
        with self.assertRaisesRegex(LocalError, "invalid_request"):
            self.inspect("health", days=0)
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        with self.assertRaisesRegex(LocalError, "denied"):
            Inspection(self.store).execute(
                reader,
                {
                    "version": 1,
                    "incarnation": self.store.incarnation,
                    "action": "health",
                    "args": {"project": "p"},
                },
            )
        self.assertEqual(
            self.browser(self.session(), "inspection", "health", {"days": 7})["window"]["days"], 7
        )

    def test_briefings_feedback_and_reach_are_counted_from_retained_records(self):
        found = self.run_r()  # the fixture briefing asks about the timeout observation
        self.assertNotEqual(found["status"], "no_relevant_memory")
        empty = self.run_r(args=self.brief_args(query="quartz lattice"))
        self.assertEqual(empty["status"], "no_relevant_memory")
        report = self.inspect("health")
        self.assertEqual(report["briefings"]["total"], 2)
        self.assertEqual(report["briefings"]["no_relevant_memory"], 1)
        self.assertEqual(report["briefings"]["no_relevant_memory_ppm"], 500000)
        self.assertEqual(report["briefings"]["deliveries_by_kind"], {"brief": 2})
        self.assertEqual(report["observations"]["delivered"], 1)
        self.assertEqual(report["observations"]["never_delivered"], 0)
        # One actor's vote counts once: its latest revision, not every revision.
        target = self.obs["observation"]
        self.feedback(found, target, use="used", usefulness="helpful")
        report = self.inspect("health")
        self.assertEqual(report["feedback"]["by_usefulness"], {"helpful": 1})
        self.assertEqual(report["feedback"]["by_use"], {"used": 1})
        self.assertEqual(report["feedback"]["helpful_ppm"], 1000000)
        self.feedback(found, target, version=1, use="read", usefulness="irrelevant")
        report = self.inspect("health")
        self.assertEqual(report["feedback"]["votes"], 1)
        self.assertEqual(report["feedback"]["by_usefulness"], {"irrelevant": 1})
        self.assertEqual(report["feedback"]["helpful_ppm"], 0)
        self.assertEqual(self.levels(report)["feedback"], "unknown")  # under the sample floor
        # The window is honoured: both briefings are minutes old, so one day still sees them.
        self.assertEqual(self.inspect("health", days=1)["briefings"]["total"], 2)
        self.assertNotIn("text", json.dumps(report["backlog"]))

    def test_backlog_ages_and_evidence_loss_follow_the_claim_lifecycle(self):
        proposed = self.propose()
        report = self.inspect("health")
        self.assertEqual(report["claims"]["by_state"], {"proposed": 1})
        self.assertEqual(report["backlog"]["proposed"], 1)
        item = report["backlog"]["items"][0]
        self.assertEqual((item["revision"], item["logical_key"]), (proposed["revision"], "timeout"))
        self.assertGreaterEqual(item["age_ms"], 0)
        self.assertEqual(report["backlog"]["oldest_age_ms"], item["age_ms"])
        self.assertEqual(self.levels(report)["backlog"], "watch")
        accepted = self.review(proposed)
        self.assertEqual(accepted["state"], "accept")
        report = self.inspect("health")
        self.assertEqual(report["claims"]["by_state"], {"accepted": 1})
        self.assertEqual(report["backlog"]["proposed"], 0)
        self.assertEqual(self.levels(report)["backlog"], "ok")
        # A claim whose evidence is purged is retired and counted as lost evidence.
        text = "Retry budget is 3 per request."
        other = self.capture("second", text)
        span = {
            "observation": other,
            "start_byte": 0,
            "end_byte": len(text.encode()),
            "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        }
        second = self.propose(self.body(text="retry=3", evidence=[span]), key="retry")
        self.review(second)
        self.assertEqual(self.inspect("health")["claims"]["families"], 2)
        self.store.purge(self.owner, other)
        report = self.inspect("health")
        self.assertEqual(report["claims"]["families"], 2)
        self.assertEqual(report["claims"]["evidence_purged"], 1)
        self.assertEqual(report["claims"]["by_state"], {"accepted": 1})
        self.assertEqual(report["observations"]["purged"], 1)
        self.assertEqual(self.levels(report)["evidence"], "watch")
        # A quarantined claim needs attention; a replaced one counts as superseded.
        self.review(accepted, decision="quarantine", version=accepted["family_version"])
        report = self.inspect("health")
        self.assertEqual(report["claims"]["quarantined"], 1)
        self.assertEqual(self.levels(report)["evidence"], "attention")


class LocalHealthTests(HealthCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown


class PostgresHealthTests(HealthCases, pg.NativeFixture):
    pass
