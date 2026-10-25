"""Harness self-tests only: these do not establish product scenario conformance."""

import json
import unittest
from pathlib import Path

from tools.shared_conformance import PROBES, EvidenceResult, suite_passed


class SharedRunnerTests(unittest.TestCase):
    def test_empty_skipped_failed_and_expected_failure_are_not_passes(self):
        empty = EvidenceResult()
        self.assertFalse(suite_passed(empty, 0))
        for method in ("skip", "fail", "expected"):
            case = unittest.FunctionTestCase(lambda: None)
            result = EvidenceResult()
            result.startTest(case)
            if method == "skip":
                result.addSkip(case, "synthetic missing runtime")
            else:
                try:
                    raise AssertionError("synthetic self-test")
                except AssertionError:
                    import sys

                    error = sys.exc_info()
                    if method == "fail":
                        result.addFailure(case, error)
                    else:
                        result.addExpectedFailure(case, error)
            result.stopTest(case)
            self.assertFalse(suite_passed(result, 1))
        passed = EvidenceResult()
        unittest.FunctionTestCase(lambda: None).run(passed)
        self.assertTrue(suite_passed(passed, 1))
        self.assertFalse(suite_passed(passed, 2))

    def test_registry_counts_match_explicit_product_probe_inventory(self):
        root = Path(__file__).resolve().parents[1]
        for scenario, names in PROBES.items():
            fixture = json.loads((root / "fixtures/scenarios" / (scenario + ".json")).read_text())
            step = fixture["steps"][0]
            self.assertEqual("shared_adapter_available", fixture["implementation_status"])
            self.assertEqual({"scenario": scenario}, step["arguments"])
            self.assertEqual(len(names), step["expect"]["tests_executed"])
            self.assertEqual(len(names), len(set(names)))
            self.assertTrue(all(name.startswith("test_") for name in names))
