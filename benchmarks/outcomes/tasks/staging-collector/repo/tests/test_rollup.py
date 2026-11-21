import unittest

from metricsd.points import make_point
from metricsd.rollup import latest, rollup


class RollupTests(unittest.TestCase):
    def setUp(self):
        self.points = [make_point("cpu", 1, 10), make_point("cpu", 3, 30), make_point("mem", 5, 20)]

    def test_rollup(self):
        result = rollup(self.points)
        self.assertEqual(list(result), ["cpu", "mem"])
        self.assertEqual(result["cpu"], {"count": 2, "min": 1, "max": 3, "mean": 2.0})

    def test_latest(self):
        self.assertEqual(latest(self.points)["cpu"]["value"], 3)

    def test_empty(self):
        self.assertEqual(rollup([]), {})
        self.assertEqual(latest([]), {})


if __name__ == "__main__":
    unittest.main()
