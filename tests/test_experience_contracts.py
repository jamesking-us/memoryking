import json
import unittest
from pathlib import Path

from memoryking.contracts.canonical import ContractError
from memoryking.contracts.validation import validate_experience


class ExperienceContractTests(unittest.TestCase):
    def test_shared_vectors(self):
        path = Path(__file__).resolve().parents[1] / "fixtures/contracts/experience-vectors.json"
        for vector in json.loads(path.read_text()):
            with self.subTest(vector["name"]):
                if vector["valid"]:
                    validate_experience(vector["request"])
                else:
                    with self.assertRaises(ContractError):
                        validate_experience(vector["request"])
