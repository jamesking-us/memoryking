"""Evaluate rules and deliver every alert through every sink."""

from alerts.rules import evaluate
from alerts.sinks.email import EmailSink
from alerts.sinks.webhook import WebhookSink


def run(samples, rules, email_transport, webhook_post):
    sinks = [EmailSink(email_transport), WebhookSink(webhook_post)]
    delivered = 0
    for alert in evaluate(samples, rules):
        for sink in sinks:
            sink.deliver(alert)
        delivered += 1
    return delivered
