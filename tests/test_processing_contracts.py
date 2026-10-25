import json
import unittest
from pathlib import Path

from memoryking.contracts.canonical import ContractError
from memoryking.contracts.validation import validate_processing


class ProcessingContractTests(unittest.TestCase):
    def test_shared_request_vectors(self):
        vectors = json.loads(
            (
                Path(__file__).resolve().parents[1] / "fixtures/contracts/processing-vectors.json"
            ).read_text()
        )
        for vector in vectors:
            with self.subTest(vector["name"]):
                if vector["valid"]:
                    validate_processing(vector["request"])
                else:
                    with self.assertRaises(ContractError):
                        validate_processing(vector["request"])
