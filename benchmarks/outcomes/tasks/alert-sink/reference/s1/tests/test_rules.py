import unittest

from alerts.rules import evaluate

RULES = [
    {"name": "cpu-high", "metric": "cpu", "op": ">", "threshold": 90, "severity": "critical"},
    {"name": "disk-low", "metric": "disk_free", "op": "<=", "threshold": 10, "severity": "warning"},
]


class RuleTests(unittest.TestCase):
    def test_fires_in_order(self):
        samples = {"b": {"cpu": 95}, "a": {"cpu": 99, "disk_free": 5}, "c": {"cpu": 10}}
        alerts = evaluate(samples, RULES)
        self.assertEqual([(a.host, a.rule) for a in alerts], [("a", "cpu-high"), ("a", "disk-low"), ("b", "cpu-high")])
        self.assertEqual(alerts[0].message, "cpu=99 > 90")

    def test_missing_metric_skipped(self):
        self.assertEqual(evaluate({"a": {"mem": 1}}, RULES), [])


if __name__ == "__main__":
    unittest.main()
