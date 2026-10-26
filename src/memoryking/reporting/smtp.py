"""Fixed-destination TLS mail adapter with explicit pre-/post-DATA outcomes.

This module has no shell-file discovery, retry loop, database or policy authority.
The caller durably claims an immutable job and supplies a current release check.
"""

from __future__ import annotations

import hashlib
import re
import smtplib
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from typing import Any, Literal

from memoryking.adapters.local_files import private_file
from memoryking.contracts.canonical import load_strict
from memoryking.domain.local import LocalError
from memoryking.domain.reporting import MIME_LIMIT

ADDRESS = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?"
)


@dataclass(frozen=True)
class GmailDestination:
    """Operator configuration, never supplied in report content or a viewer route."""

    id: str
    sender: str = field(repr=False)
    recipient: str = field(repr=False)
    password: str = field(repr=False)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", self.id):
            raise LocalError("invalid_report_destination")
        for address in (self.sender, self.recipient):
            if len(address) > 254 or not ADDRESS.fullmatch(address):
                raise LocalError("invalid_report_destination")
        if not self.password or len(self.password) > 512 or any(ord(c) < 32 for c in self.password):
            raise LocalError("invalid_report_mail_secret")

    @property
    def fingerprint(self) -> str:
        # Domain is case-insensitive; local part is retained exactly.
        local, domain = self.recipient.rsplit("@", 1)
        return hashlib.sha256((local + "@" + domain.lower()).encode()).hexdigest()

    @classmethod
    def from_file(cls, path: Path) -> GmailDestination:
        private_file(path)
        if path.stat().st_size > 4096:
            raise LocalError("invalid_report_mail_secret")
        value = load_strict(path.read_bytes())
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "destination", "GMAIL_FROM", "GMAIL_APP_KEY", "KINDLE_TO"}
            or value["version"] != 1
            or any(
                not isinstance(value[k], str)
                for k in ("destination", "GMAIL_FROM", "GMAIL_APP_KEY", "KINDLE_TO")
            )
        ):
            raise LocalError("invalid_report_mail_secret")
        return cls(
            value["destination"], value["GMAIL_FROM"], value["KINDLE_TO"], value["GMAIL_APP_KEY"]
        )


def message(
    destination: GmailDestination,
    *,
    delivery: str,
    report: str,
    revision: int,
    title: str,
    epub: bytes,
    created_ms: int,
) -> bytes:
    if (
        not re.fullmatch(r"[a-f0-9]{64}", delivery)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", report)
        or type(revision) is not int
        or revision < 1
        or not title
        or len(title) > 512
        or any(ord(c) < 32 or ord(c) == 127 for c in title)
    ):
        raise LocalError("invalid_report_mail_message")
    if len(epub) > MIME_LIMIT:
        raise LocalError("report_mime_limit")
    mail = EmailMessage(policy=policy.SMTP)
    mail["From"], mail["To"] = destination.sender, destination.recipient
    mail["Subject"] = title
    mail["Message-ID"] = f"<mk-{delivery}@memoryking.invalid>"
    mail["Date"] = format_datetime(datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc))
    mail.set_content("Your MemoryKing report is attached as a Kindle-friendly EPUB.\n")
    mail.add_attachment(
        epub, maintype="application", subtype="epub+zip", filename=f"{report}-r{revision}.epub"
    )
    # Stable MIME bytes are useful reconciliation evidence; never rely on a
    # Message-ID or boundary to make a provider deduplicate submission.
    mail.set_boundary("mk-" + delivery)
    raw = mail.as_bytes()
    if len(raw) > MIME_LIMIT:
        raise LocalError("report_mime_limit")
    return raw


@dataclass(frozen=True)
class Outcome:
    state: Literal["smtp_accepted", "rejected", "pre_data_transient", "unknown"]
    stage: str
    code: int | None = None


def submit(
    destination: GmailDestination,
    raw: bytes,
    authorize: Callable[[], Callable[[], None]],
    *,
    smtp_factory: Callable[..., Any] = smtplib.SMTP_SSL,
) -> Outcome:
    """Exactly one attempt. The owner of the durable job decides any future retry.

    authorize persists the release instant under the canonical gate and returns a
    deadline checker. Check immediately before DATA and again before content bytes.
    A possible partial body write is unknown. QUIT cannot erase observed acceptance.
    """
    if len(raw) > MIME_LIMIT:
        raise LocalError("report_mime_limit")
    client: Any = None
    stage = "connect"
    body_started = False
    try:
        client = smtp_factory(
            "smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=10
        )
        stage = "authentication"
        client.login(destination.sender, destination.password)
        stage = "sender"
        code, _ = client.mail(destination.sender)
        if code != 250:
            return Outcome("pre_data_transient" if 400 <= code < 500 else "rejected", stage, code)
        stage = "recipient"
        code, _ = client.rcpt(destination.recipient)
        if code not in {250, 251}:
            return Outcome("pre_data_transient" if 400 <= code < 500 else "rejected", stage, code)
        stage = "authorization"
        check = authorize()
        check()
        stage = "data_command"
        code, _ = client.docmd("DATA")
        if code != 354:
            return Outcome("pre_data_transient" if 400 <= code < 500 else "rejected", stage, code)
        stage = "release_deadline"
        check()
        # RFC 5321 transparency: quote leading dots, terminate exactly once.
        if b"\n" in raw.replace(b"\r\n", b"") or not raw.endswith(b"\r\n"):
            raise LocalError("invalid_report_mail_message")
        wire = re.sub(rb"(?m)^\.", b"..", raw) + b".\r\n"
        stage, body_started = "data_body", True
        client.send(wire)
        stage = "data_reply"
        code, _ = client.getreply()
        if code == 250:
            return Outcome("smtp_accepted", stage, code)
        return Outcome("rejected", stage, code)
    except smtplib.SMTPAuthenticationError as exc:
        return Outcome("rejected", stage, int(exc.smtp_code))
    except smtplib.SMTPResponseException as exc:
        if body_started:
            return Outcome("unknown", stage)
        code = int(exc.smtp_code)
        return Outcome("pre_data_transient" if 400 <= code < 500 else "rejected", stage, code)
    except (OSError, smtplib.SMTPException):
        return Outcome("unknown" if body_started else "pre_data_transient", stage)
    finally:
        if client is not None:
            # close performs no network exchange, avoiding a second ambiguous
            # operation after a definitive DATA reply (or an incomplete DATA body).
            try:
                client.close()
            except (OSError, smtplib.SMTPException):
                pass
