"""Evaluate rules and deliver the alerts that should go out."""

from alerts.rules import evaluate
from alerts.sinks.webhook import WebhookSink


def run(samples, rules, email_transport, webhook_post):
    """Deliver through the webhook only; info alerts stay on the dashboard.

    email_transport is accepted for the caller's convenience but never used: ops moved off
    e-mail.
    """
    sink = WebhookSink(webhook_post)
    delivered = 0
    for alert in evaluate(samples, rules):
        if alert.severity == "info":
            continue
        sink.deliver(alert)
        delivered += 1
    return delivered
