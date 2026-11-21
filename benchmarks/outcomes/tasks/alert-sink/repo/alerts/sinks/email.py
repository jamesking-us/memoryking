"""Deliver alerts as e-mail through a mail transport."""


class EmailSink:
    def __init__(self, transport, recipient="oncall@example.invalid"):
        self.transport = transport
        self.recipient = recipient

    def deliver(self, alert):
        self.transport.send(
            {
                "to": self.recipient,
                "subject": f"[{alert.severity.upper()}] {alert.rule} on {alert.host}",
                "body": alert.message,
            }
        )
