"""Both-store verified legacy identity reservation; no historical content or sends."""

import copy
import json
import unittest
from contextlib import contextmanager
from dataclasses import replace

import test_knowledge as knowledge
from recovery_helpers import backup_fixture, restore_fixture
from test_postgres_parity import NativeFixture
from test_report_delivery import DeliveryCases
from test_report_smtp import FakeSMTP
from test_report_store import ReportCases

from memoryking.adapters.report_delivery import Deliveries
from memoryking.domain.local import LocalError


class ImportCases:
    command = ReportCases.command
    source = ReportCases.source
    policy = DeliveryCases.policy

    def prepared(self):
        self.policy()
        self.store.report_mail = replace(self.store.report_mail, enabled=False)
        evidence = self.source("legacy-proof", b"Synthetic accepted legacy receipt")
        return {
            "report": "old-phase",
            "legacy": {
                "version": 1,
                "program": "synthetic",
                "phase": "S01",
                "revision": 2,
                "legacy_identity": "a" * 64,
                "receipt_sha256": evidence["sha256"],
                "source_sha256": "b" * 64,
                "artifact_sha256": "c" * 64,
                "message_id": "<synthetic-accepted@reports.invalid>",
                "accepted_ms": 1,
                "recipient_fingerprint": self.store.report_mail.destination.fingerprint,
                "destination": "synthetic",
                "evidence": evidence,
            },
        }

    def test_import_reserves_identity_without_app_content_or_email(self):
        args = self.prepared()
        result = self.command("import_delivery", args, key="legacy-import")
        self.assertTrue(result["imported"])
        self.assertEqual(result["state"], "smtp_accepted")
        self.assertEqual(result["device_delivery"], "unverified")
        for table in ("report_revisions", "report_files"):
            self.assertEqual(
                self.store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0
            )
        repeated = self.command("import_delivery", args, key="legacy-import")
        self.assertEqual(repeated["delivery"], result["delivery"])
        self.assertEqual(self.command("import_delivery", args)["delivery"], result["delivery"])
        fake = FakeSMTP()
        self.store.report_mail = replace(self.store.report_mail, enabled=True)
        receipt = Deliveries(self.store).attempt(
            self.owner, "p", result["delivery"], smtp_factory=fake.factory
        )
        self.assertEqual(receipt["state"], "smtp_accepted")
        self.assertEqual(fake.events, [])
        self.assertEqual(self.command("list", {"limit": 50, "cursor": None})["reports"], [])

    def test_import_is_operator_only_requires_disabled_sender_and_exact_evidence(self):
        args = self.prepared()
        author = self.store.grant(self.owner, "author", "p", ["read", "report_author"])
        with self.assertRaises(LocalError):
            self.command("import_delivery", args, author)
        with self.assertRaises(LocalError):
            self.command("sender_status", {}, author)
        status = self.command("sender_status", {})
        self.assertTrue(status["configured"])
        self.assertFalse(status["enabled"])
        self.assertNotIn("@", json.dumps(status))
        self.store.report_mail = replace(self.store.report_mail, enabled=True)
        with self.assertRaises(LocalError) as denied:
            self.command("import_delivery", args)
        self.assertEqual(denied.exception.code, "report_import_requires_disabled_sender")
        self.store.report_mail = replace(self.store.report_mail, enabled=False)
        for change in (
            {"receipt_sha256": "d" * 64},
            {"recipient_fingerprint": "d" * 64},
            {"accepted_ms": 9007199254740991},
        ):
            with self.assertRaises(LocalError):
                self.command("import_delivery", {**args, "legacy": {**args["legacy"], **change}})

    def test_changed_payload_or_title_alias_cannot_duplicate_legacy_identity(self):
        args = self.prepared()
        self.command("import_delivery", args, key="original")
        changed = copy.deepcopy(args)
        changed["legacy"]["artifact_sha256"] = "d" * 64
        for key in ("original", "different-key"):
            with self.assertRaises(LocalError):
                self.command("import_delivery", changed, key=key)
        with self.assertRaises(LocalError):
            self.command("import_delivery", {**args, "report": "same-title-another-id"})
        with self.assertRaises(LocalError):
            self.command(
                "import_delivery",
                {**args, "legacy": {**args["legacy"], "legacy_identity": "e" * 64}},
            )
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM report_deliveries").fetchone()[0], 1
        )

    def test_import_survives_older_restore_and_reserves_revision(self):
        args = self.prepared()
        config = self.store.report_mail
        old = backup_fixture(self)
        result = self.command("import_delivery", args)
        restore_fixture(self, old)
        self.store.report_mail = config
        self.assertEqual(
            self.command("delivery_status", {"delivery": result["delivery"]})["state"],
            "smtp_accepted",
        )
        self.assertEqual(self.command("import_delivery", args)["delivery"], result["delivery"])
        # An older acknowledged report revision cannot be allocated again, even
        # though this content archive predates its imported independent receipt.
        with self.assertRaises(LocalError) as conflict:
            self.command(
                "submit",
                {
                    "report": "old-phase",
                    "expected_revision": 0,
                    "upload": "none",
                    "program": "synthetic",
                    "phase": "S01",
                    "task": None,
                    "title": "Synthetic phase",
                    "technical_status": "partial",
                    "classification": "internal",
                    "dependencies": [args["legacy"]["evidence"]],
                },
            )
        self.assertEqual(conflict.exception.code, "revision_conflict")
        fake = FakeSMTP()
        self.assertEqual(
            Deliveries(self.store).attempt(
                self.owner, "p", result["delivery"], smtp_factory=fake.factory
            )["state"],
            "smtp_accepted",
        )
        self.assertEqual(fake.events, [])


class LocalImport(ImportCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown

    @contextmanager
    def actor(self, credential):
        yield self.store


class NativeImport(ImportCases, NativeFixture):
    pass
