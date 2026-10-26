"""Native stores and independent control history with fake SMTP only."""

import json
import os
import platform
import unittest
from contextlib import contextmanager

import test_knowledge as knowledge
from recovery_helpers import backup_fixture, restore_fixture
from test_postgres_parity import NativeFixture
from test_report_smtp import FakeSMTP
from test_report_store import ReportCases

from memoryking.adapters.report_delivery import Deliveries, MailConfiguration
from memoryking.domain.local import LocalError, SystemClock
from memoryking.reporting.smtp import GmailDestination


class AdvanceClock(SystemClock):
    offset = 0

    def utc_ms(self):
        return super().utc_ms() + self.offset

    def monotonic_ms(self):
        return super().monotonic_ms() + self.offset


class DeliveryCases:
    command = ReportCases.command
    source = ReportCases.source
    upload = ReportCases.upload
    submitted = ReportCases.submitted
    build = ReportCases.build
    qa = ReportCases.qa

    def policy(self, readers=None, version=0):
        self.store.report_mail = MailConfiguration(
            GmailDestination(
                "synthetic", "sender@example.invalid", "kindle@example.invalid", "fake-secret"
            ),
            enabled=True,
        )
        return self.command(
            "policy",
            {
                "expected_version": version,
                "policy": {
                    "version": 1,
                    "project": "p",
                    "program": "synthetic",
                    "template": "phase-report",
                    "channel": getattr(self, "channel", "both"),
                    "destination": "synthetic",
                    "recipient_principal": getattr(self, "recipient", "owner"),
                    "require_visual_qa": True,
                    "require_evidence": True,
                    "classification": "restricted",
                    "readers": readers or ["owner"],
                },
            },
        )

    def publish(self):
        self.submitted()
        self.build()
        self.qa()
        self.command("publish", {"report": "phase", "revision": 1})
        return self.store.db.execute("SELECT id FROM report_deliveries").fetchone()[0]

    def attempt(self, delivery, client):
        return Deliveries(self.store).attempt(
            self.owner, "p", delivery, smtp_factory=client.factory
        )

    def viewer_recipient_job(self):
        from memoryking.adapters.viewer_identity import ViewerIdentity

        for action, args in (
            ("bind", {"principal": "phone", "issuer": "https://github.com", "user_id": "12345"}),
            (
                "grant",
                {
                    "principal": "phone",
                    "project": "p",
                    "expected_version": 1,
                    "starts": None,
                    "expires": None,
                },
            ),
        ):
            ViewerIdentity(self.store).execute(
                self.owner,
                {
                    "version": 1,
                    "incarnation": self.store.incarnation,
                    "key": "viewer-" + action,
                    "action": action,
                    "args": args,
                },
            )
        self.recipient = "phone"
        self.submitted(readers=["owner", "phone"])
        self.build()
        self.qa()
        self.command("publish", {"report": "phase", "revision": 1})
        return self.store.db.execute("SELECT id FROM report_deliveries").fetchone()[0]

    def test_bound_viewer_recipient_can_receive_without_issuing_browser_session(self):
        job = self.viewer_recipient_job()
        smtp = FakeSMTP()
        self.assertEqual(self.attempt(job, smtp)["state"], "smtp_accepted")
        self.assertIn("body", smtp.events)

    def test_disabled_viewer_recipient_blocks_dispatch_before_smtp(self):
        job = self.viewer_recipient_job()
        from memoryking.adapters.viewer_identity import ViewerIdentity

        ViewerIdentity(self.store).execute(
            self.owner,
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "key": "disable-viewer-recipient",
                "action": "disable",
                "args": {"principal": "phone", "expected_version": 2},
            },
        )
        smtp = FakeSMTP()
        with self.assertRaisesRegex(LocalError, "denied"):
            self.attempt(job, smtp)
        self.assertEqual(smtp.events, [])
        status = self.command("delivery_status", {"delivery": job})
        self.assertEqual(status["attempts"], 0)

    def test_reconciliation_requires_evidence_and_records_a_durable_receipt(self):
        job = self.publish()
        self.assertEqual(self.attempt(job, FakeSMTP(fail="reply"))["state"], "unknown")
        args = {
            "delivery": job,
            "expected_attempts": 1,
            "outcome": "smtp_accepted",
            "evidence": {"kind": "external_source", "id": "provider-proof", "sha256": "a" * 64},
        }
        with self.assertRaises(LocalError):
            self.command("reconcile", args)
        args["evidence"] = self.source("provider-proof", b"Synthetic provider acceptance evidence")
        result = self.command("reconcile", args, key="reconcile-accepted")
        self.assertEqual(result["state"], "smtp_accepted")
        self.assertEqual(result["outcome_source"], "operator_reconciliation")
        self.assertIsInstance(result["outcome_ms"], int)
        self.assertEqual(
            self.command("reconcile", args, key="reconcile-accepted")["state"], "smtp_accepted"
        )
        unused = FakeSMTP()
        self.assertEqual(self.attempt(job, unused)["state"], "smtp_accepted")
        self.assertEqual(unused.events, [])

    def test_proven_nonsubmission_requires_explicit_operator_reconciliation_before_retry(self):
        job = self.publish()
        self.assertEqual(self.attempt(job, FakeSMTP(fail="body"))["state"], "unknown")
        proof = self.source("provider-proof", b"Synthetic independently verified nonsubmission")
        result = self.command(
            "reconcile",
            {
                "delivery": job,
                "expected_attempts": 1,
                "outcome": "not_submitted",
                "evidence": proof,
            },
        )
        self.assertEqual(result["state"], "queued")
        self.assertEqual(result["attempts"], 1)
        accepted = self.attempt(job, FakeSMTP())
        self.assertEqual(accepted["state"], "smtp_accepted")
        self.assertEqual(accepted["outcome_source"], "data_reply")
        self.assertEqual(accepted["attempts"], 2)

    def test_stale_reconciliation_cannot_match_a_later_authorized_cycle(self):
        job = self.publish()
        self.assertEqual(self.attempt(job, FakeSMTP(fail="reply"))["state"], "unknown")
        proof = self.source(
            "first-attempt-proof", b"Synthetic observed first attempt nonsubmission"
        )
        original = {
            "delivery": job,
            "expected_attempts": 1,
            "outcome": "not_submitted",
            "evidence": proof,
        }
        self.command("reconcile", original, key="first-reconciliation")
        later = self.attempt(job, FakeSMTP(fail="reply"))
        self.assertEqual((later["state"], later["attempts"]), ("unknown", 2))
        with self.assertRaises(LocalError) as stale:
            self.command("reconcile", original, key="stale-new-key")
        self.assertEqual(stale.exception.code, "report_reconciliation_conflict")
        self.command("reconcile", original, key="first-reconciliation")
        self.assertEqual(self.command("delivery_status", {"delivery": job})["state"], "unknown")
        unused = FakeSMTP()
        self.assertEqual(self.attempt(job, unused)["state"], "unknown")
        self.assertEqual(unused.events, [])

    def test_reconciled_cycle_keeps_three_attempt_budget_without_reusing_identity(self):
        job = self.publish()
        self.store.clock = AdvanceClock()
        self.attempt(job, FakeSMTP(fail="body"))
        self.command(
            "reconcile",
            {
                "delivery": job,
                "expected_attempts": 1,
                "outcome": "not_submitted",
                "evidence": self.source("proof", b"Synthetic nonsubmission evidence"),
            },
        )
        for expected, offset, state in [
            (2, 0, "queued"),
            (3, 1001, "queued"),
            (4, 5001, "rejected"),
        ]:
            self.store.clock.offset += offset
            result = self.attempt(job, FakeSMTP(fail="connect"))
            self.assertEqual((result["attempts"], result["state"]), (expected, state))

    def test_ambiguous_experimental_reset_history_is_not_guessed(self):
        job = self.publish()
        self.attempt(job, FakeSMTP(fail="reply"))
        proof = self.source("old-proof", b"Synthetic historical nonsubmission")
        with self.store.transaction() as decision:
            who = self.store._auth(self.owner, "report_policy", "p", decision.evaluated_ms)
            ledger = Deliveries(self.store)
            body = ledger._job(who["tenant"], job)
            del body["cycle_attempts"]
            body["reconciliation"].append({"outcome": "not_submitted"})
            ledger._write(who, body, "synthetic_old_format", decision.evaluated_ms)
        with self.assertRaises(LocalError) as ambiguous:
            self.command(
                "reconcile",
                {
                    "delivery": job,
                    "expected_attempts": 1,
                    "outcome": "not_submitted",
                    "evidence": proof,
                },
            )
        self.assertEqual(ambiguous.exception.code, "report_delivery_attempt_history_required")
        unused = FakeSMTP()
        self.assertEqual(self.attempt(job, unused)["state"], "unknown")
        self.assertEqual(unused.events, [])

    def test_kindle_only_policy_queues_delivery_without_app_visibility(self):
        self.channel = "kindle"
        job = self.publish()
        self.assertEqual(self.command("list", {"limit": 50, "cursor": None})["reports"], [])
        with self.assertRaises(LocalError):
            self.command("reader", {"report": "phase", "revision": 1, "page": 0, "cursor": None})
        receipt = self.attempt(job, FakeSMTP())
        self.assertEqual(receipt["state"], "smtp_accepted")
        self.assertEqual(receipt["device_delivery"], "unverified")

    def test_reduction_ordered_after_release_instant_does_not_erase_acceptance(self):
        job = self.publish()
        parent = self

        class RevokeAfterRelease(FakeSMTP):
            def docmd(self, command):
                result = super().docmd(command)
                parent.source("evidence", b'{"result":"synthetic"}', enabled=False, version=1)
                return result

        client = RevokeAfterRelease()
        self.assertEqual(self.attempt(job, client)["state"], "smtp_accepted")
        self.assertIsNotNone(client.sent)

    def test_expired_release_deadline_after_data_prompt_requeues_without_content(self):
        job = self.publish()
        self.store.clock = AdvanceClock()
        parent = self

        class LateDataPrompt(FakeSMTP):
            def docmd(self, command):
                result = super().docmd(command)
                parent.store.clock.offset += 6000
                return result

        client = LateDataPrompt()
        self.assertEqual(self.attempt(job, client)["state"], "queued")
        self.assertIsNone(client.sent)

    def test_provider_acceptance_is_durable_and_duplicate_attempt_does_not_send(self):
        job = self.publish()
        client = FakeSMTP(fail="close")
        result = self.attempt(job, client)
        self.assertEqual(result["state"], "smtp_accepted")
        self.assertEqual(result["device_delivery"], "unverified")
        unused = FakeSMTP()
        self.assertEqual(self.attempt(job, unused), result)
        self.assertEqual(unused.events, [])
        body = json.loads(self.store.db.execute("SELECT body FROM report_deliveries").fetchone()[0])
        self.assertGreater(body["release"]["deadline_ms"], body["release"]["evaluated_ms"])
        self.assertNotIn("@example.invalid", json.dumps(body))
        self.assertEqual(body["attempts"], 1)

    def test_proven_pre_data_transients_have_bounded_backoff_and_three_attempts(self):
        job = self.publish()
        self.store.clock = AdvanceClock()
        first = FakeSMTP(fail="connect")
        self.assertEqual(self.attempt(job, first)["state"], "queued")
        with self.assertRaises(LocalError) as early:
            self.attempt(job, FakeSMTP())
        self.assertEqual(early.exception.code, "report_delivery_backoff")
        self.store.clock.offset += 1001
        self.assertEqual(self.attempt(job, FakeSMTP(fail="connect"))["state"], "queued")
        self.store.clock.offset += 5001
        self.assertEqual(self.attempt(job, FakeSMTP(fail="connect"))["state"], "rejected")
        unused = FakeSMTP()
        self.assertEqual(self.attempt(job, unused)["state"], "rejected")
        self.assertEqual(unused.events, [])

    def test_unknown_survives_older_content_restore_and_never_retries(self):
        job = self.publish()
        archive = backup_fixture(self)
        result = self.attempt(job, FakeSMTP(fail="reply"))
        self.assertEqual(result["state"], "unknown")
        restore_fixture(self, archive)
        unused = FakeSMTP()
        self.assertEqual(self.attempt(job, unused)["state"], "unknown")
        self.assertEqual(unused.events, [])

    def test_accepted_survives_older_content_restore_and_never_retries(self):
        job = self.publish()
        archive = backup_fixture(self)
        self.assertEqual(self.attempt(job, FakeSMTP())["state"], "smtp_accepted")
        restore_fixture(self, archive)
        unused = FakeSMTP()
        self.assertEqual(self.attempt(job, unused)["state"], "smtp_accepted")
        self.assertEqual(unused.events, [])

    def test_crash_after_provider_reply_recovers_to_unknown(self):
        job = self.publish()

        def fault(name):
            if name == "report_mail_after_provider":
                raise RuntimeError("synthetic process interruption")

        self.store.fault = fault
        with self.assertRaises(RuntimeError):
            self.attempt(job, FakeSMTP())
        self.store.fault = lambda _: None
        self.assertEqual(
            self.store.db.execute("SELECT state FROM report_deliveries").fetchone()[0], "submitting"
        )
        unused = FakeSMTP()
        self.assertEqual(self.attempt(job, unused)["state"], "unknown")
        self.assertEqual(unused.events, [])

    def test_recipient_source_revocation_before_release_sends_no_content(self):
        job = self.publish()
        parent = self

        class RevokeAtRecipient(FakeSMTP):
            def rcpt(self, recipient):
                result = super().rcpt(recipient)
                parent.source("evidence", b'{"result":"synthetic"}', enabled=False, version=1)
                return result

        client = RevokeAtRecipient()
        self.assertEqual(self.attempt(job, client)["state"], "rejected")
        self.assertIsNone(client.sent)
        self.assertNotIn("DATA", client.events)

    def test_failed_publication_journal_intent_cannot_send_an_unpublished_report(self):
        self.submitted()
        self.build()
        self.qa()

        def fault(name):
            if name == "report_before_publication_commit":
                raise LocalError("injected_publication_failure")

        self.store.fault = fault
        with self.assertRaises(LocalError):
            self.command("publish", {"report": "phase", "revision": 1})
        self.store.fault = lambda _: None
        # Replay is the normal startup boundary after a durable journal append
        # and failed content commit; neither side is hand-edited to force success.
        from test_postgres_parity import InspectionSQL

        from memoryking.adapters.local_store import LocalStore
        from memoryking.adapters.postgres_store import PostgresStore

        config = self.store.report_mail
        native = isinstance(self.store, PostgresStore)
        self.store.close()
        if native:
            self.store = PostgresStore(self.pool, self.data, self.control, self.tenant, self.owner)
            self.store.db = InspectionSQL(self.store, self.store.db)
        else:
            self.store = LocalStore(self.data, self.control)
        self.store.report_mail = config
        job = self.store.db.execute("SELECT id FROM report_deliveries").fetchone()[0]
        client = FakeSMTP()
        with self.assertRaises(LocalError) as denied:
            self.attempt(job, client)
        self.assertEqual(denied.exception.code, "report_not_published")
        self.assertEqual(client.events, [])


@unittest.skipUnless(
    platform.system() == "Darwin"
    or (
        platform.system() == "Linux" and os.environ.get("MEMORYKING_REQUIRE_REPORT_SANDBOX") == "1"
    ),
    "native renderer not selected",
)
class LocalDelivery(DeliveryCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown

    @contextmanager
    def actor(self, credential):
        yield self.store


@unittest.skipUnless(
    platform.system() == "Darwin"
    or (
        platform.system() == "Linux" and os.environ.get("MEMORYKING_REQUIRE_REPORT_SANDBOX") == "1"
    ),
    "native renderer not selected",
)
class NativeDelivery(DeliveryCases, NativeFixture):
    pass
