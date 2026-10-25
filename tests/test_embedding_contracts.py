import json
import unittest
from pathlib import Path

from memoryking.contracts.canonical import ContractError
from memoryking.contracts.validation import validate_embedding


class EmbeddingContractTests(unittest.TestCase):
    def test_shared_request_vectors(self):
        vectors = json.loads(
            (
                Path(__file__).resolve().parents[1] / "fixtures/contracts/embedding-vectors.json"
            ).read_text()
        )
        for vector in vectors:
            with self.subTest(vector["name"]):
                if vector["valid"]:
                    validate_embedding(vector["request"])
                else:
                    with self.assertRaises(ContractError):
                        validate_embedding(vector["request"])
