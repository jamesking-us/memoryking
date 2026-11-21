import unittest

from metricsd.batch import chunks


class BatchTests(unittest.TestCase):
    def test_chunks(self):
        self.assertEqual([len(c) for c in chunks(range(120))], [50, 50, 20])
        self.assertEqual(list(chunks([], 3)), [])
        self.assertEqual(list(chunks([1, 2, 3], 2)), [[1, 2], [3]])

    def test_bad_size(self):
        with self.assertRaises(ValueError):
            list(chunks([1], 0))


if __name__ == "__main__":
    unittest.main()
