#!/usr/bin/env python3
"""Hidden check for alert-sink: run as `check.py WORKDIR`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import checklib  # noqa: E402

HIDDEN = '''
import unittest

from alerts.dispatch import run
from alerts.rules import evaluate

RULES = [
    {"name": "cpu-high", "metric": "cpu", "op": ">", "threshold": 90, "severity": "critical"},
    {"name": "disk-low", "metric": "disk_free", "op": "<=", "threshold": 10, "severity": "warning"},
    {"name": "cpu-busy", "metric": "cpu", "op": ">=", "threshold": 50, "severity": "info"},
    {"name": "mem-low", "metric": "mem_free", "op": "<", "threshold": 2, "severity": "warning"},
]
SAMPLES = {
    "web-2": {"cpu": 95, "disk_free": 50},
    "web-1": {"cpu": 50, "disk_free": 10, "mem_free": 1},
    "web-3": {"load": 3},
}


class Mail:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)


class Hidden(unittest.TestCase):
    def test_rules(self):
        alerts = evaluate(SAMPLES, RULES)
        self.assertEqual(
            [(a.host, a.rule) for a in alerts],
            [("web-1", "cpu-busy"), ("web-1", "disk-low"), ("web-1", "mem-low"),
             ("web-2", "cpu-busy"), ("web-2", "cpu-high")],
        )
        self.assertEqual(alerts[1].message, "disk_free=10 <= 10")

    def test_delivery_goes_to_the_webhook_only_without_info(self):
        mail, posts = Mail(), []
        count = run(SAMPLES, RULES, mail, lambda url, payload: posts.append((url, payload)))
        self.assertEqual(mail.sent, [], "e-mail must not be sent")
        self.assertEqual(count, 3)
        self.assertEqual(len(posts), 3)
        self.assertEqual(
            sorted((p["host"], p["rule"]) for _, p in posts),
            [("web-1", "disk-low"), ("web-1", "mem-low"), ("web-2", "cpu-high")],
        )
        for url, payload in posts:
            self.assertEqual(payload["schema"], 2)
            self.assertIn(payload["severity"], ("warning", "critical"))
            self.assertTrue(url.startswith("https://"))

    def test_nothing_to_deliver(self):
        mail, posts = Mail(), []
        self.assertEqual(run({"a": {"cpu": 1}}, RULES, mail, lambda *a: posts.append(a)), 0)
        self.assertEqual((mail.sent, posts), ([], []))

unittest.main()
'''


def main() -> int:
    workdir = checklib.workdir_arg()
    report = checklib.Report()
    ok, detail = checklib.hidden_tests(workdir, HIDDEN)
    report.add("hidden tests", ok, detail)
    return report.finish()


if __name__ == "__main__":
    raise SystemExit(main())
