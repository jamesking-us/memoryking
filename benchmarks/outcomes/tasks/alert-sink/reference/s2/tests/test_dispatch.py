import unittest

from alerts.dispatch import run

RULES = [
    {"name": "cpu-high", "metric": "cpu", "op": ">", "threshold": 90, "severity": "critical"},
    {"name": "cpu-note", "metric": "cpu", "op": ">", "threshold": 50, "severity": "info"},
]


class Mail:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)


class DispatchTests(unittest.TestCase):
    def test_webhook_only_and_no_info(self):
        mail, posts = Mail(), []
        count = run({"a": {"cpu": 95}}, RULES, mail, lambda url, payload: posts.append(payload))
        self.assertEqual(count, 1)
        self.assertEqual(mail.sent, [])
        self.assertEqual([p["rule"] for p in posts], ["cpu-high"])


if __name__ == "__main__":
    unittest.main()
