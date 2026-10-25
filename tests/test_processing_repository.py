"""Code-bound optional explanations use the same current eligibility proofs."""

import json
import unittest

import test_repository as repository
import test_retrieval as retrieval
import test_working as working
from test_processing import ProcessingCases

from memoryking.domain.local import LocalError


class ProcessingRepositoryCases:
    run_p = ProcessingCases.run_p
    compile = ProcessingCases.compile
    scope = working.WorkingTests.scope
    command = working.WorkingTests.command
    save = working.WorkingTests.save

    def test_code_change_omits_dependent_summary_without_removing_unrelated_knowledge(self):
        self.bind()
        self.verify()
        independent = self.review(self.propose(self.body("Independent fact"), key="independent"))
        self.save()
        result = self.compile(bindings=[self.binding()], claims=[self.claim["revision"]])
        self.assertEqual(result["optional_explanation"]["mode"], "extractive_fallback")
        deps = json.loads(
            self.store.db.execute(
                "SELECT dependencies FROM rendered_artifacts WHERE id=?", (result["receipt"],)
            ).fetchone()[0]
        )
        self.assertIn("code_validation", [d["kind"] for d in deps])
        (self.repo / "a.py").write_text("def value():\n    return 9\n")
        changed = self.capture()
        # No worker completion is required to reject unseen-current guidance.
        current = self.compile(bindings=[self.binding(changed)], claims=[self.claim["revision"]])
        self.assertEqual(current["optional_explanation"]["mode"], "omitted")
        self.assertEqual(current["checkpoint"]["state"], result["checkpoint"]["state"])
        self.worker(changed)
        eligible = self.query(bindings=[self.binding(changed)])
        self.assertEqual([i["revision"] for i in eligible["items"]], [independent["revision"]])
        # A historical exact replay stays tied to its original snapshot. Quarantine
        # subsequently denies even that artifact via its claim dependency closure.
        self.review(self.claim, decision="quarantine", version=2)
        with self.assertRaises(LocalError):
            self.run_r("replay", {"project": "p", "receipt": result["receipt"]})


class LocalProcessingRepositoryTests(ProcessingRepositoryCases, unittest.TestCase):
    setUp = repository.RepositoryTests.setUp
    tearDown = repository.RepositoryTests.tearDown
    execute = working.WorkingTests.execute
    run_r = retrieval.RetrievalTests.run_r


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
    setattr(LocalProcessingRepositoryTests, name, getattr(repository.RepositoryTests, name))
