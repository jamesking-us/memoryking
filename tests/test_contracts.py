import json
import subprocess
import sys
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from memoryking.contracts import ContractError, canonical_bytes, load_strict, validate_contract
from memoryking.contracts.validation import schema

ROOT = Path(__file__).resolve().parents[1]


class ContractTests(unittest.TestCase):
    def test_cross_language_acceptance_and_exact_hashes(self):
        python = subprocess.check_output([sys.executable, "tools/contract_vectors.py"], cwd=ROOT)
        node = subprocess.check_output(["node", "tools/contract-vectors.mjs"], cwd=ROOT)
        self.assertEqual(json.loads(python), json.loads(node))

    def test_schema_itself_is_valid(self):
        Draft202012Validator.check_schema(schema())

    def test_bounded_and_nonfinite_values(self):
        for value in [float("nan"), float("inf"), 1.0, {1: "x"}, "\ud800", 2**53]:
            with self.subTest(value=repr(value)), self.assertRaises(ContractError):
                canonical_bytes(value)
        with self.assertRaises(ContractError):
            load_strict('"' + "x" * 1_048_576 + '"')
        with self.assertRaises(ContractError):
            load_strict("[" * 66 + "0" + "]" * 66)

    def test_unicode_boundaries_keys_and_nested_values_keep_canonical_bytes(self):
        valid = ["\ud7ff", "\ue000", "\U0001f418", "long text " * 10000]
        value = {"boundary": valid, "\U0001f418": {"value": "\ud7ff\ue000"}}
        expected = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.assertEqual(canonical_bytes(value), expected)
        for bad in ("\ud800", "\udfff", "\ud83d\udc18", "prefix\ud900suffix"):
            for value in (bad, {bad: "value"}, {"nested": [bad]}):
                with (
                    self.subTest(value=repr(value)),
                    self.assertRaisesRegex(ContractError, "^invalid_unicode$"),
                ):
                    canonical_bytes(value)

    def test_incarnation_and_counter_are_not_floats(self):
        event = json.loads((ROOT / "fixtures/contracts/event.json").read_text())
        validate_contract("ObservationEvent", event)
        event["source_position"]["position"] = 9007199254740993
        with self.assertRaises(ContractError):
            validate_contract("ObservationEvent", event)

    def test_error_does_not_echo_restricted_input(self):
        with self.assertRaisesRegex(ContractError, "^schema_invalid$"):
            validate_contract("ErrorEnvelope", {"secret": "restricted source body"})


if __name__ == "__main__":
    unittest.main()
