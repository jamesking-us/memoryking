"""Procedure drafts from repeated recorded experience, on both backends."""

import copy
import hashlib
import json
import unittest
from pathlib import Path

import test_knowledge as knowledge
import test_postgres_parity as pg
from test_experience import ProcedureCases

from memoryking.adapters.knowledge_store import Knowledge
from memoryking.adapters.outcome_drafts import METHOD, normal
from memoryking.adapters.working import Working
from memoryking.domain.local import LocalError

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures/contracts/checkpoint.json").read_text()
)


class OutcomeDraftCases:
    body = ProcedureCases.body
    run_k = ProcedureCases.run_k
    run_e = ProcedureCases.run_e
    review_p = ProcedureCases.review_p
    activate_p = ProcedureCases.activate_p
    query_p = ProcedureCases.query_p

    def note(self, key, text):
        found = self.store.capture(
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
        )
        return found["observation"], text

    def cite(self, observation, text):
        return {
            "observation": observation,
            "start_byte": 0,
            "end_byte": len(text.encode()),
            "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        }

    def checkpoint(self, task, action, cites, readers=("owner",), run=None):
        body = copy.deepcopy(FIXTURE)
        body["failed_attempts"] = [
            {
                "action": action,
                "outcome": "Exceeded the operation deadline",
                "do_not_repeat_when": "The total deadline is unchanged",
            }
        ]
        body["references"] = [{"kind": "observation", "id": o} for o in cites]
        command = {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": "save",
            "key": f"ckpt-{task}-{run}-{abs(hash(action))}",
            "args": {
                "project": "p",
                "task": task,
                "run": run,
                "expected_revision": 0,
                "readers": list(readers),
                "checkpoint": body,
            },
        }
        if hasattr(self, "service"):
            return self.service(Working, command)
        output = []
        Working(self.store).execute(self.owner, command).emit(output.append)
        return json.loads(output[0])

    def result(self, task, result, evidence=(), **changes):
        body = {
            "task": task,
            "run": "run1",
            "result": result,
            "acceptance_evidence": [self.cite(o, t) for o, t in evidence],
            "failures": [],
            "rework": [],
            "elapsed_ms": None,
            "input_tokens": None,
            "cached_input_tokens": None,
            "output_tokens": None,
            "limitations": ["Recorded by hand for the test"],
            "readers": None,
            **changes,
        }
        return self.run_e(
            "outcome",
            {
                "project": "p",
                "logical_key": f"outcome-{task}",
                "expected_revision": 0,
                "body": body,
            },
        )

    def run_draft(self, **args):
        self.n += 1
        command = {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": "draft",
            "key": f"draft{self.n}",
            "args": {"project": "p", **args},
        }
        if hasattr(self, "service"):
            return self.service(Knowledge, command)
        output = []
        Knowledge(self.store).execute(self.owner, command).emit(output.append)
        return json.loads(output[0])

    def procedures(self):
        rows = self.store.db.execute(
            "SELECT id,body FROM revisions WHERE kind='procedure' ORDER BY created"
        ).fetchall()
        return [
            {
                "revision": row["id"],
                "state": self.k._state(
                    self.store.db.execute("SELECT tenant FROM tenants").fetchone()[0],
                    row["id"],
                    9_007_199_254_740_991,
                )[0],
                "body": json.loads(row["body"]),
            }
            for row in rows
        ]

    def test_normalisation_ignores_case_punctuation_and_spacing(self):
        self.assertEqual(normal("Increase  EVERY timeout."), normal("increase every timeout"))
        self.assertNotEqual(normal("Increase every timeout"), normal("Increase one timeout"))

    def test_a_failed_attempt_recurring_across_tasks_becomes_an_avoid_procedure_for_review(self):
        fail1 = self.note("f1", "Timeouts raised everywhere; the batch still hit the deadline.")
        fail2 = self.note(
            "f2", "Raised all timeouts to 120s and the request deadline was exceeded."
        )
        good = self.note(
            "g1", "Per-attempt timeout with a total deadline passed the acceptance run."
        )
        first = self.checkpoint("task-a", "Increase every timeout", [fail1[0]])
        second = self.checkpoint("task-b", "increase  every timeout.", [fail2[0]])
        self.result("task-a", "success", [good])
        report = self.run_draft()
        procedures = report["procedures"]
        self.assertEqual(procedures["method"], METHOD)
        self.assertEqual(len(procedures["drafted"]), 1)
        (item,) = self.procedures()
        self.assertEqual(item["state"], "proposed")
        self.assertEqual(item["body"]["provenance"]["method"], METHOD)
        self.assertEqual(item["body"]["provenance"]["polarity"], "avoid")
        self.assertEqual(item["body"]["provenance"]["tasks"], 2)
        self.assertEqual(
            sorted(item["body"]["provenance"]["checkpoints"]),
            sorted([first["checkpoint"], second["checkpoint"]]),
        )
        procedure = item["body"]["procedure"]
        self.assertTrue(procedure["steps"][0].startswith("Do not: "))
        self.assertEqual(procedure["success_evidence"], [good[0]])
        self.assertEqual(sorted(procedure["failure_evidence"]), sorted([fail1[0], fail2[0]]))
        # Nothing activates itself: it needs a review and then a separate activation.
        self.assertEqual(self.query_p()["items"], [])
        reviewed = self.review_p({"revision": item["revision"], "family_version": 1})
        self.assertEqual(self.query_p()["items"], [])
        self.assertEqual(self.query_p()["blockers"][0]["reason"], "activation_required")
        with self.assertRaises(LocalError):
            self.activate_p({"revision": item["revision"], "family_version": 99})
        self.assertEqual(reviewed["state"], "accept")
        # Running it again files nothing new.
        again = self.run_draft()
        self.assertEqual(again["procedures"]["drafted"], [])
        self.assertEqual(again["procedures"]["skipped"], {"already_proposed": 1})

    def test_only_independent_tasks_count_as_repetition(self):
        fail1 = self.note("f1", "Raised every timeout and the deadline was still exceeded.")
        good = self.note("g1", "Acceptance run passed with a per-attempt timeout.")
        self.checkpoint("task-a", "Increase every timeout", [fail1[0]], run="r1")
        self.checkpoint("task-a", "Increase every timeout", [fail1[0]], run="r2")
        self.result("task-a", "success", [good])
        # Two streams of one task are one task, not independent repetition.
        self.assertEqual(self.run_draft()["procedures"]["drafted"], [])
        # A second task repeats it, but nothing shows what worked instead.
        self.checkpoint("task-b", "Increase every timeout", [fail1[0]])
        report = self.run_draft(procedures=True)
        self.assertEqual(report["procedures"]["drafted"][0]["polarity"], "avoid")
        self.assertEqual(self.procedures()[0]["state"], "proposed")

    def test_without_success_evidence_or_with_a_private_checkpoint_nothing_is_filed(self):
        fail1 = self.note("f1", "Raised every timeout and the deadline was still exceeded.")
        self.checkpoint("task-a", "Increase every timeout", [fail1[0]])
        self.checkpoint("task-b", "Increase every timeout", [fail1[0]])
        report = self.run_draft()
        self.assertEqual(report["procedures"]["drafted"], [])
        self.assertEqual(report["procedures"]["skipped"], {"no_success_evidence": 1})
        good = self.note("g1", "Acceptance run passed with a per-attempt timeout.")
        self.result("task-a", "success", [good])
        # The checkpoint of task-b is only readable by someone else: one task remains.
        self.store.db.execute(
            "UPDATE checkpoints SET readers=? WHERE task='task-b'", ('["somebody"]',)
        )
        report = self.run_draft()
        self.assertEqual(report["procedures"]["drafted"], [])

    def test_a_rework_step_recurring_in_successful_outcomes_becomes_a_do_procedure(self):
        one = self.note("s1", "Tests passed after the lockfile was regenerated first.")
        two = self.note("s2", "Build was green once the lockfile had been regenerated.")
        step = "Regenerate the lockfile before running the tests"
        self.result("task-a", "success", [one], rework=[step])
        self.result("task-b", "success", [two], rework=[step.upper() + "."])
        report = self.run_draft()
        self.assertEqual([d["polarity"] for d in report["procedures"]["drafted"]], ["do"])
        (item,) = self.procedures()
        self.assertTrue(item["body"]["text"].startswith("Do: "))
        self.assertEqual(item["body"]["procedure"]["failure_evidence"], [])
        self.assertEqual(
            sorted(item["body"]["procedure"]["success_evidence"]), sorted([one[0], two[0]])
        )

    def test_the_same_anchors_group_differently_worded_attempts(self):
        fail1 = self.note(
            "f1", "Retrying forever left payments/retry.py spinning past the deadline."
        )
        fail2 = self.note("f2", "payments/retry.py never gave up and the job missed its deadline.")
        good = self.note("g1", "Capped retries in payments/retry.py and the acceptance run passed.")
        self.checkpoint("task-a", "Edited payments/retry.py to retry forever", [fail1[0]])
        self.checkpoint("task-b", "Changed payments/retry.py so retries never stop", [fail2[0]])
        self.result("task-b", "success", [good])
        report = self.run_draft()
        self.assertEqual(len(report["procedures"]["drafted"]), 1)
        self.assertEqual(self.procedures()[0]["body"]["provenance"]["tasks"], 2)

    def test_procedure_drafts_can_be_switched_off(self):
        fail1 = self.note("f1", "Raised every timeout and the deadline was still exceeded.")
        good = self.note("g1", "Acceptance run passed with a per-attempt timeout.")
        self.checkpoint("task-a", "Increase every timeout", [fail1[0]])
        self.checkpoint("task-b", "Increase every timeout", [fail1[0]])
        self.result("task-a", "success", [good])
        report = self.run_draft(procedures=False)
        self.assertNotIn("procedures", report)
        self.assertEqual(self.procedures(), [])


class LocalOutcomeDraftTests(OutcomeDraftCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown


class PostgresOutcomeDraftTests(OutcomeDraftCases, pg.NativeFixture):
    pass


if __name__ == "__main__":
    unittest.main()
