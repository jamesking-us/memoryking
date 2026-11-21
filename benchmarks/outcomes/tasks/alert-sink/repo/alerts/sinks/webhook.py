"""Deliver alerts to the incident webhook (payload schema version 2)."""

SCHEMA = 2
DEFAULT_URL = "https://hooks.example.invalid/incidents"


class WebhookSink:
    def __init__(self, post, url=DEFAULT_URL):
        self.post = post
        self.url = url

    def deliver(self, alert):
        self.post(
            self.url,
            {
                "schema": SCHEMA,
                "rule": alert.rule,
                "severity": alert.severity,
                "host": alert.host,
                "text": alert.message,
            },
        )
