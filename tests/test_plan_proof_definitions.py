"""The faster history comparison preserves the original proof binding."""

import copy
import unittest

from test_plans import plan

from memoryking.domain.plans import definition, proof_definitions


class ProofDefinitionTests(unittest.TestCase):
    def test_normalized_comparison_matches_original_binding(self):
        original = plan()
        original["steps"][0].update(
            status="done", evidence=[{"kind": "observation", "id": "first"}]
        )
        variants = [copy.deepcopy(original) for _ in range(14)]
        variants[0]["title"] = "Presentation only"
        variants[1]["steps"].reverse()
        variants[2]["steps"][0]["notes"] = "Presentation only"
        variants[3]["objective"] = "Changed goal"
        variants[4]["steps"][0]["description"] = "Changed prerequisite"
        variants[5]["steps"][0]["phase"] = "verify"
        variants[6]["steps"][0]["status"] = "doing"
        variants[7]["steps"][0]["decision"] = "Changed decision"
        variants[8]["steps"][0]["evidence"].append({"kind": "observation", "id": "second"})
        variants[9]["steps"][1]["description"] = "Changed own definition"
        variants[10]["steps"][1]["depends_on"] = []
        variants[11]["steps"][1]["status"] = "done"
        variants[12]["steps"][0]["evidence"] *= 2
        variants[13]["steps"][1]["depends_on"] *= 2
        expected = proof_definitions(original, ["inspect", "test"])
        for number, variant in enumerate(variants):
            for key, actual in proof_definitions(variant, expected).items():
                with self.subTest(variant=number, step=key):
                    self.assertEqual(
                        actual == expected[key],
                        definition(variant, key) == definition(original, key),
                    )

    def test_transitive_changes_and_reordering(self):
        original = plan()
        final = copy.deepcopy(original["steps"][1])
        final.update(id="finish", depends_on=["test"])
        original["steps"].append(final)
        changed = copy.deepcopy(original)
        changed["steps"][0]["decision"] = "Changed transitive prerequisite"
        self.assertNotEqual(
            proof_definitions(original, ["finish"]),
            proof_definitions(changed, ["finish"]),
        )
        changed = copy.deepcopy(original)
        changed["steps"].reverse()
        self.assertEqual(
            proof_definitions(original, ["finish"]),
            proof_definitions(changed, ["finish"]),
        )
