"""The same plan progress, conflict and disclosure cases on both real backends."""

import copy
import json
import unittest
from unittest.mock import patch

import test_knowledge as knowledge
import test_postgres_parity as pg

from memoryking.adapters.working import MUTATIONS, Working
from memoryking.domain.local import LocalError


def plan():
    return {
        "title": "Reliable checkout",
        "objective": "Never charge twice",
        "context": "",
        "status": "active",
        "closure_note": "",
        "lessons": [],
        "phases": [{"id": "design", "title": "Design"}, {"id": "verify", "title": "Verify"}],
        "steps": [
            {
                "id": "inspect",
                "phase": "design",
                "description": "Inspect retry behavior",
                "status": "todo",
                "notes": "",
                "depends_on": [],
                "evidence": [],
                "decision": None,
            },
            {
                "id": "test",
                "phase": "verify",
                "description": "Test duplicate retry",
                "status": "todo",
                "notes": "",
                "depends_on": ["inspect"],
                "evidence": [],
                "decision": None,
            },
        ],
    }


class PlanCases:
    def command(self, action, args, key=None):
        self.n += 1
        c = {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": action,
            "args": {"project": "p", **args},
        }
        if action in MUTATIONS:
            c["key"] = key or f"plan-{self.n}"
        return c

    def execute(self, command, credential=None):
        output = []
        Working(self.store).execute(credential or self.owner, command).emit(output.append)
        return json.loads(output[0])

    def save_plan(self, value=None, revision=0, task="checkout", **kw):
        return self.execute(
            self.command(
                "plan_save",
                {
                    "task": task,
                    "expected_revision": revision,
                    "readers": ["owner"],
                    "plan": value or plan(),
                    **kw,
                },
            )
        )

    def get_plan(self, task="checkout", revision=None):
        return self.execute(self.command("plan_get", {"task": task, "revision": revision}))

    def proof(self):
        return [{"kind": "observation", "id": self.obs["observation"]}]

    def step(self, step="inspect", revision=1, status="done", **kw):
        return self.execute(
            self.command(
                "plan_step",
                {
                    "task": "checkout",
                    "step": step,
                    "expected_revision": revision,
                    "status": status,
                    "notes": "Recorded actual test output",
                    "evidence": self.proof(),
                    "decision": None,
                    **kw,
                },
            )
        )

    def test_create_progress_step_update_history_and_reorder(self):
        self.save_plan()
        self.assertEqual(self.get_plan()["progress"]["work_percent"], 0)
        self.step()
        current = self.get_plan()
        self.assertEqual(current["progress"]["completed"], 1)
        self.assertEqual(current["progress"]["work_percent"], 50)
        self.assertEqual(current["progress"]["current_phase"], "verify")
        self.assertEqual(self.get_plan(revision=1)["progress"]["completed"], 0)
        body = plan()
        body["steps"][0].update(status="done", evidence=self.proof())
        body["phases"].reverse()
        body["steps"].reverse()
        self.save_plan(body, 2)
        self.assertEqual(self.get_plan()["progress"]["completed"], 1)

    def test_empty_skipped_and_reported_lifecycle_are_not_completed_work(self):
        value = plan()
        value["steps"] = []
        self.save_plan(value)
        self.assertIsNone(self.get_plan()["progress"]["work_percent"])
        value = plan()
        for s in value["steps"]:
            s.update(status="skipped", notes="Out of agreed scope", decision="Owner narrowed scope")
        self.save_plan(value, 1)
        result = self.get_plan()
        self.assertEqual(result["progress"]["work_percent"], 0)
        self.assertEqual(result["progress"]["resolved_percent"], 100)
        self.assertEqual(result["plan"]["status"], "active")

    def test_stale_revision_and_retry_cannot_double_apply(self):
        command = self.command(
            "plan_save",
            {"task": "checkout", "expected_revision": 0, "readers": ["owner"], "plan": plan()},
            "same-plan",
        )
        a = self.execute(command)
        b = self.execute(command)
        self.assertEqual(a["checkpoint"], b["checkpoint"])
        self.assertTrue(b["replayed"])
        with self.assertRaisesRegex(LocalError, "checkpoint_revision_conflict"):
            self.save_plan(revision=0)
        bad = copy.deepcopy(command)
        bad["args"]["plan"]["title"] = "Different"
        with self.assertRaisesRegex(LocalError, "idempotency_conflict"):
            self.execute(bad)

    def test_cycles_missing_ids_and_unresolved_prerequisites_rejected(self):
        for mutation, error in [
            (lambda p: p["steps"][0].update(depends_on=["test"]), "plan_dependency_cycle"),
            (lambda p: p["steps"][1].update(depends_on=["absent"]), "plan_reference_invalid"),
            (lambda p: p["steps"][1].update(id="inspect"), "plan_duplicate_id"),
            (lambda p: p["steps"][1].update(status="doing"), "plan_dependency_unresolved"),
            (lambda p: p["steps"][0].update(status="done"), "verification_evidence_required"),
            (lambda p: p["steps"][0].update(status="blocked"), "plan_reason_required"),
        ]:
            value = plan()
            mutation(value)
            with self.subTest(error=error), self.assertRaisesRegex(LocalError, error):
                self.save_plan(value)

    def test_complete_requires_steps_and_lesson(self):
        value = plan()
        value["status"] = "done"
        with self.assertRaisesRegex(LocalError, "plan_unresolved_steps"):
            self.save_plan(value)
        for s in value["steps"]:
            s.update(status="done", evidence=self.proof())
        with self.assertRaisesRegex(LocalError, "plan_closure_evidence_required"):
            self.save_plan(value)
        value.update(lessons=self.proof(), closure_note="Tests and lesson recorded")
        self.save_plan(value)
        self.assertEqual(self.get_plan()["progress"]["work_percent"], 100)

    def test_definition_change_requires_fresh_proof_and_reset_does_not_launder_it(self):
        self.save_plan()
        self.step()
        value = plan()
        value["steps"][0].update(
            status="done", evidence=self.proof(), description="New requirement"
        )
        with self.assertRaisesRegex(LocalError, "plan_reverification_required"):
            self.save_plan(value, 2)
        self.step(revision=2, status="todo", evidence=[])
        self.save_plan(value, 3)
        result = self.get_plan()
        self.assertEqual(result["plan"]["steps"][0]["reported_status"], "done")
        self.assertEqual(result["plan"]["steps"][0]["status"], "unknown")
        self.assertEqual(result["progress"]["completed"], 0)

    def test_pagination_is_complete_and_changed_frontier_fences_cursor(self):
        for name in ("a", "b", "c"):
            self.save_plan(task=name)
        args = {"limit": 2, "cursor": None}
        first = self.execute(self.command("plan_list", args))
        self.assertEqual([p["id"] for p in first["plans"]], ["a", "b"])
        second = self.execute(self.command("plan_list", {**args, "cursor": first["cursor"]}))
        self.assertEqual([p["id"] for p in second["plans"]], ["c"])
        self.assertTrue(second["complete"])
        self.save_plan(task="d")
        with self.assertRaisesRegex(LocalError, "context_changed"):
            self.execute(self.command("plan_list", {**args, "cursor": first["cursor"]}))

    def test_reordering_dropping_or_duplicating_links_is_not_fresh_evidence(self):
        second = self.store.capture(
            self.owner,
            self.ns,
            "second-proof",
            {
                "version": 1,
                "project": "p",
                "source": "test",
                "source_event": "second-proof",
                "text": "Another historical result",
                "readers": None,
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )
        proofs = [*self.proof(), {"kind": "observation", "id": second["observation"]}]
        value = plan()
        value["steps"][0].update(status="done", evidence=proofs)
        self.save_plan(value)
        for refs in (list(reversed(proofs)), proofs[:1], [*proofs, proofs[0]]):
            changed = copy.deepcopy(value)
            changed["steps"][0].update(description="A new requirement", evidence=refs)
            with self.assertRaisesRegex(LocalError, "plan_reverification_required"):
                self.save_plan(changed, 1)
        reopened = copy.deepcopy(value)
        reopened["steps"][0].update(status="todo", description="A new requirement", evidence=[])
        self.save_plan(reopened, 1)
        reopened["steps"][0].update(status="done", evidence=list(reversed(proofs)))
        self.save_plan(reopened, 2)
        self.assertEqual(self.get_plan()["progress"]["unknown"], 1)

    def test_notes_beyond_128_revisions_do_not_erase_unchanged_completion(self):
        value = plan()
        value["steps"][0].update(status="done", evidence=self.proof())
        self.save_plan(value)
        for revision in range(1, 131):
            value["steps"][0]["notes"] = f"Progress note {revision}"
            self.save_plan(value, revision)
        current = self.get_plan()
        self.assertTrue(current["history_complete"])
        self.assertEqual(current["progress"]["completed"], 1)

    def test_history_checks_each_shared_dependency_once_per_read(self):
        value = plan()
        value["steps"][0].update(status="done", evidence=self.proof())
        self.save_plan(value)
        for revision in range(1, 21):
            value["steps"][0]["notes"] = f"Progress note {revision}"
            self.save_plan(value, revision)
        original = Working._dependency
        calls = []

        def checked(working, who, dependency, at):
            calls.append((dependency["kind"], dependency["id"]))
            return original(working, who, dependency, at)

        with patch.object(Working, "_dependency", checked):
            current = self.get_plan()
        self.assertEqual(current["revision"], 21)
        self.assertTrue(current["history_complete"])
        self.assertEqual(current["progress"]["completed"], 1)
        self.assertEqual(calls, [("observation", self.obs["observation"])])
        # A later request must authorize again, including on the same adapter.
        working = Working(self.store)
        working.execute(self.owner, self.command("plan_get", {"task": "checkout", "revision": None}))
        self.store.purge(self.owner, self.obs["observation"])
        with self.assertRaisesRegex(LocalError, "checkpoint_(evidence_)?unavailable"):
            working.execute(self.owner, self.command("plan_get", {"task": "checkout", "revision": None}))

    def test_owner_reassignment_exposes_the_current_write_revision(self):
        self.save_plan()
        self.execute(
            self.command(
                "assign_writer",
                {
                    "task": "checkout",
                    "run": None,
                    "expected_revision": 1,
                    "writer": "owner",
                    "reason": "explicit_ownership_reconciliation",
                },
            )
        )
        read = self.get_plan()
        self.assertEqual(read["revision"], 1)
        self.assertEqual(read["current_revision"], 2)
        self.step(revision=read["current_revision"])
        self.assertEqual(self.get_plan()["progress"]["completed"], 1)

    def test_purged_evidence_cannot_leave_plan_or_aggregate_visible(self):
        self.save_plan()
        self.step()
        self.store.purge(self.owner, self.obs["observation"])
        with self.assertRaises(LocalError):
            self.get_plan()
        result = self.execute(self.command("plan_list", {"limit": 10, "cursor": None}))
        self.assertEqual(result["plans"], [])
        self.save_plan(revision=2)
        self.assertEqual(self.get_plan()["progress"]["completed"], 0)


class LocalPlanTests(PlanCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown


class PostgresPlanTests(PlanCases, pg.NativeFixture):
    pass
