import unittest

from metricsd import envutil, points


class BasicsTests(unittest.TestCase):
    def test_make_point(self):
        self.assertEqual(points.make_point("cpu", 0.5, 10), {"name": "cpu", "value": 0.5, "ts": 10})

    def test_invalid_points(self):
        with self.assertRaises(ValueError):
            points.make_point("", 1)
        with self.assertRaises(ValueError):
            points.make_point("x", "1")
        with self.assertRaises(ValueError):
            points.make_point("x", True)

    def test_flags(self):
        self.assertTrue(envutil.flag_enabled({"F": "1"}, "F"))
        self.assertTrue(envutil.flag_enabled({"F": "True"}, "F"))
        self.assertFalse(envutil.flag_enabled({}, "F"))
        self.assertFalse(envutil.flag_enabled({"F": "0"}, "F"))

    def test_require(self):
        self.assertEqual(envutil.require({"K": "v"}, "K"), "v")
        with self.assertRaises(RuntimeError):
            envutil.require({}, "K")


if __name__ == "__main__":
    unittest.main()
