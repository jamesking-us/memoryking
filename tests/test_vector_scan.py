"""The compact vector form and the matrix scan, without a store or a model."""

import random
import unittest

from memoryking.domain.embeddings import cosine_ppm, pack, quantize, unpack
from memoryking.domain.local import LocalError


class VectorStorageTests(unittest.TestCase):
    def test_packed_vectors_round_trip_within_a_five_digit_error(self):
        vector = quantize([0.3, -0.5, 0.1, 0.7, -0.2, 0.05], 6)
        packed = pack(vector)
        self.assertEqual(len(packed), 12)  # two bytes a dimension
        self.assertTrue(
            all(abs(a - b) <= 20 for a, b in zip(vector, unpack(packed, 6), strict=True))
        )
        with self.assertRaises(LocalError):
            unpack(packed, 5)

    def test_matrix_scan_ranks_like_the_exact_integer_cosine(self):
        try:
            import numpy
        except ImportError:
            self.skipTest("numpy is part of the optional embeddings extra")
        from memoryking.adapters.dense_search import _scan_numpy

        rng = random.Random(3)
        dimensions = 32
        rows, expected = [], {}
        query = quantize([rng.gauss(0, 1) for _ in range(dimensions)], dimensions)
        for number in range(300):
            vector = quantize([rng.gauss(0, 1) for _ in range(dimensions)], dimensions)
            subject = f"obs{number % 120}"  # several parts for some subjects
            rows.append({"subject": subject, "vector": pack(vector)})
            expected[subject] = max(expected.get(subject, -(10**9)), cosine_ppm(query, vector))
        found = _scan_numpy(numpy, query, rows, [], dimensions, -1000000)
        self.assertEqual(set(found), set(expected))
        for subject, score in expected.items():
            self.assertLessEqual(abs(found[subject] - score), 50)  # sixteen-bit storage

        best = max(expected, key=lambda s: (expected[s], s))
        self.assertIn(best, sorted(found, key=lambda s: (-found[s], s))[:3])


if __name__ == "__main__":
    unittest.main()
