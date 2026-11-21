import unittest

from alerts.model import Alert
from alerts.sinks.email import EmailSink
from alerts.sinks.webhook import WebhookSink


class Mail:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)


class SinkTests(unittest.TestCase):
    def setUp(self):
        self.alert = Alert("cpu-high", "critical", "web-1", "cpu=95 > 90")

    def test_email(self):
        mail = Mail()
        EmailSink(mail).deliver(self.alert)
        self.assertEqual(mail.sent[0]["subject"], "[CRITICAL] cpu-high on web-1")

    def test_webhook(self):
        posts = []
        WebhookSink(lambda url, payload: posts.append((url, payload))).deliver(self.alert)
        self.assertEqual(posts[0][1]["schema"], 2)
        self.assertEqual(posts[0][1]["host"], "web-1")

    def test_unknown_severity(self):
        with self.assertRaises(ValueError):
            Alert("r", "loud", "h", "m")


if __name__ == "__main__":
    unittest.main()
