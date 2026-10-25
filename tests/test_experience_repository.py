"""Current repository evidence suspends a previously activated procedure."""

import unittest

import test_repository as repository
from test_experience import ProcedureCases

from memoryking.domain.local import LocalError


class ProcedureRepositoryCases:
    run_e = ProcedureCases.run_e
    propose_p = ProcedureCases.propose_p
    review_p = ProcedureCases.review_p
    activate_p = ProcedureCases.activate_p
    query_p = ProcedureCases.query_p
    run_r = ProcedureCases.run_r
    brief_args = ProcedureCases.brief_args

    def test_changed_dependency_withholds_active_procedure_before_worker_runs(self):
        reviewed = self.review_p(self.propose_p(procedure_changes={"dependency_mode": "required"}))
        self.bind(reviewed)
        with self.assertRaisesRegex(LocalError, "procedure_validation_required"):
            self.activate_p(reviewed, bindings=[self.binding()])
        self.verify(reviewed)
        self.activate_p(reviewed, bindings=[self.binding()])
        active = self.query_p(bindings=[self.binding()])
        self.assertEqual(active["items"][0]["effective_state"], "active")
        brief = self.run_r(args=self.brief_args(bindings=[self.binding()]))
        self.assertIn(reviewed["revision"], [i["reference"] for i in brief["guidance"]])
        (self.repo / "a.py").write_text("def value():\n    return 77\n")
        changed = self.capture()
        self.assertEqual(self.query_p(bindings=[self.binding(changed)])["items"], [])
        item = self.query_p(bindings=[self.binding(changed)], diagnostic=True)["items"][0]
        self.assertEqual(item["effective_state"], "suspended")
        current = self.run_r(args=self.brief_args(bindings=[self.binding(changed)]))
        self.assertNotIn(reviewed["revision"], [i["reference"] for i in current["guidance"]])
        # Old exact replay remains tied to its old snapshot, not a current assertion.
        self.assertEqual(self.run_r("replay", {"project": "p", "receipt": brief["receipt"]}), brief)


class LocalProcedureRepositoryTests(ProcedureRepositoryCases, unittest.TestCase):
    setUp = repository.RepositoryTests.setUp
    tearDown = repository.RepositoryTests.tearDown


for name in (
    "run_k",
    "body",
    "propose",
    "review",
    "query",
    "git",
    "run_repo",
    "capture",
    "binding",
    "bind",
    "verify",
    "worker",
):
    setattr(LocalProcedureRepositoryTests, name, getattr(repository.RepositoryTests, name))
