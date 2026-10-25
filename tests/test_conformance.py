import json
import unittest
from dataclasses import asdict
from pathlib import Path

from memoryking.application.conformance import run_scenario


class HarnessSelfTestAdapter:
    name = "harness-self-test-only"
    supported_scenarios = frozenset({"SELF"})

    def reset(self, seed):
        self.value = seed["value"]

    def execute(self, operation, arguments):
        return {"value": self.value + arguments["increment"]}


class ConformanceTests(unittest.TestCase):
    def test_harness_detects_failure_and_missing_adapter(self):
        case = {
            "id": "SELF",
            "seed": {"value": 2},
            "steps": [{"operation": "add", "arguments": {"increment": 3}, "expect": {"value": 5}}],
        }
        self.assertEqual(run_scenario(case, HarnessSelfTestAdapter()).status, "passed")
        case["steps"][0]["expect"]["value"] = 6
        self.assertEqual(run_scenario(case, HarnessSelfTestAdapter()).status, "failed")
        self.assertEqual(run_scenario(case, None).status, "not_evaluated")

    def test_inventory_distinguishes_available_adapter_from_execution(self):
        root = Path(__file__).resolve().parents[1]
        paths = list((root / "fixtures/scenarios").glob("*.json"))
        expected = {f"S{i:02}" for i in range(1, 37)} | {f"DB{i:02}" for i in range(1, 13)}
        cases = [json.loads(path.read_text()) for path in paths]
        self.assertEqual({c["id"] for c in cases}, expected)
        requirements = set()
        for case in cases:
            requirements.update(case["requirements"])
            self.assertEqual(
                case["implementation_status"],
                "local_adapter_available"
                if case["id"]
                in {
                    "S01",
                    "S02",
                    "S05",
                    "S06",
                    "S07",
                    "S13",
                    "S14",
                    "S15",
                    "S16",
                    "S20",
                    "S21",
                    "S22",
                    "S25",
                    "S27",
                }
                else "shared_adapter_available"
                if case["id"]
                in {
                    "S03",
                    "S04",
                    "S11",
                    "S19",
                    "S12",
                    "S08",
                    "S09",
                    "S10",
                    "S30",
                    "S17",
                    "S18",
                    "S23",
                    "S24",
                    "S28",
                    "S32",
                    "S33",
                    "S35",
                    "S36",
                }
                else "not_evaluated",
            )
            self.assertTrue(case["steps"])
            self.assertTrue(any(step["expect"] for step in case["steps"]))
            self.assertEqual(asdict(run_scenario(case, None))["status"], "not_evaluated")
        self.assertEqual(requirements, {f"R{i:02}" for i in range(1, 13)})
