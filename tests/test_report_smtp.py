"""Fake SMTP only. No credential discovery, network or messages to a real provider."""

import smtplib
import ssl
import tempfile
import unittest
from pathlib import Path

from memoryking.adapters.local_files import atomic
from memoryking.domain.local import LocalError
from memoryking.reporting.smtp import GmailDestination, message, submit


class FakeSMTP:
    def __init__(self, fail=None, data_reply=250, recipient=250, data_command=354):
        self.fail = fail
        self.data_reply, self.recipient, self.data_command = data_reply, recipient, data_command
        self.events = []
        self.sent = None

    def factory(self, host, port, **options):
        assert (host, port) == ("smtp.gmail.com", 465)
        assert options["context"].check_hostname
        assert options["context"].verify_mode == ssl.CERT_REQUIRED
        self.step("connect")
        return self

    def step(self, name):
        self.events.append(name)
        if self.fail == name:
            raise OSError("synthetic transport failure")
        if self.fail == "credentials" and name == "login":
            raise smtplib.SMTPAuthenticationError(535, b"synthetic credential refusal")

    def login(self, sender, password):
        self.step("login")

    def mail(self, sender):
        self.step("mail")
        return 250, b"fixture"

    def rcpt(self, recipient):
        self.step("rcpt")
        return self.recipient, b"fixture"

    def docmd(self, command):
        assert command == "DATA"
        self.step("DATA")
        return self.data_command, b"fixture"

    def send(self, raw):
        self.sent = raw
        self.step("body")

    def getreply(self):
        self.step("reply")
        return self.data_reply, b"fixture"

    def close(self):
        self.step("close")


class MailBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.destination = GmailDestination(
            "test", "sender@example.invalid", "kindle@example.invalid", "synthetic-test-password"
        )
        self.raw = message(
            self.destination,
            delivery="a" * 64,
            report="synthetic",
            revision=1,
            title="Synthetic report",
            epub=b"PK synthetic epub bytes",
            created_ms=1700000000000,
        )

    def test_tls_fixed_envelope_stable_mime_and_accepted_reply(self):
        client = FakeSMTP(fail="close")

        def authorize():
            client.events.append("authorization_committed")
            return lambda: client.events.append("deadline_checked")

        outcome = submit(self.destination, self.raw, authorize, smtp_factory=client.factory)
        self.assertEqual(outcome.state, "smtp_accepted")
        self.assertEqual(outcome.code, 250)
        self.assertEqual(
            client.events,
            [
                "connect",
                "login",
                "mail",
                "rcpt",
                "authorization_committed",
                "deadline_checked",
                "DATA",
                "deadline_checked",
                "body",
                "reply",
                "close",
            ],
        )
        self.assertTrue(client.sent.endswith(b"\r\n.\r\n"))
        self.assertNotIn("synthetic-test-password", repr(self.destination))
        self.assertNotIn("sender@example.invalid", repr(self.destination))
        self.assertEqual(
            self.raw,
            message(
                self.destination,
                delivery="a" * 64,
                report="synthetic",
                revision=1,
                title="Synthetic report",
                epub=b"PK synthetic epub bytes",
                created_ms=1700000000000,
            ),
        )

    def test_proven_pre_data_transient_and_credential_failure(self):
        for stage in ("connect", "login", "mail", "rcpt", "DATA"):
            client = FakeSMTP(fail=stage)
            outcome = submit(
                self.destination, self.raw, lambda: lambda: None, smtp_factory=client.factory
            )
            self.assertEqual(outcome.state, "pre_data_transient", stage)
            self.assertIsNone(client.sent)
        client = FakeSMTP(fail="credentials")
        self.assertEqual(
            submit(
                self.destination, self.raw, lambda: lambda: None, smtp_factory=client.factory
            ).state,
            "rejected",
        )
        self.assertNotIn("DATA", client.events)

    def test_possible_body_write_or_lost_reply_is_unknown_without_retry(self):
        for stage in ("body", "reply"):
            client = FakeSMTP(fail=stage)
            outcome = submit(
                self.destination, self.raw, lambda: lambda: None, smtp_factory=client.factory
            )
            self.assertEqual(outcome.state, "unknown")
            self.assertEqual(client.events.count("DATA"), 1)
        client = FakeSMTP(data_reply=550)
        self.assertEqual(
            submit(
                self.destination, self.raw, lambda: lambda: None, smtp_factory=client.factory
            ).state,
            "rejected",
        )
        self.assertEqual(client.events.count("DATA"), 1)

    def test_expiry_after_354_sends_no_content(self):
        client = FakeSMTP()

        def authorize():
            def check():
                if "DATA" in client.events:
                    raise LocalError("release_expired")

            return check

        with self.assertRaises(LocalError) as expired:
            submit(self.destination, self.raw, authorize, smtp_factory=client.factory)
        self.assertEqual(expired.exception.code, "release_expired")
        self.assertIsNone(client.sent)
        self.assertEqual(client.events[-1], "close")

    def test_mime_limit_and_header_injection_are_blocked(self):
        with self.assertRaises(LocalError) as size:
            message(
                self.destination,
                delivery="a" * 64,
                report="synthetic",
                revision=1,
                title="Synthetic",
                epub=b"x" * 18_000_000,
                created_ms=1700000000000,
            )
        self.assertEqual(size.exception.code, "report_mime_limit")
        with self.assertRaises(LocalError):
            message(
                self.destination,
                delivery="a" * 64,
                report="synthetic",
                revision=1,
                title="Synthetic\r\nBcc: victim@example.invalid",
                epub=b"PK",
                created_ms=1700000000000,
            )
        with self.assertRaises(LocalError):
            GmailDestination("test", "x@example.invalid", "victim@example.invalid\n", "fixture")

    def test_secret_file_is_explicit_closed_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mail.json"
            atomic(
                path,
                b'{"version":1,"destination":"test","GMAIL_FROM":"sender@example.invalid","GMAIL_APP_KEY":"fixture","KINDLE_TO":"kindle@example.invalid"}',
            )
            loaded = GmailDestination.from_file(path)
            self.assertEqual(loaded.fingerprint, self.destination.fingerprint)
            path.chmod(0o644)
            with self.assertRaises(LocalError):
                GmailDestination.from_file(path)
            path.chmod(0o600)
            atomic(path, b'{"version":1,"smtp_host":"evil.invalid"}')
            with self.assertRaises(LocalError):
                GmailDestination.from_file(path)
