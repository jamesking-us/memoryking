"""Explicit scheduler configuration, live builds and isolated fake-mail attempts."""

import json
import os
import platform
import tempfile
import time
import unittest
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import test_knowledge as knowledge
from test_postgres_parity import NativeFixture
from test_report_delivery import DeliveryCases
from test_report_smtp import FakeSMTP
from test_report_store import ReportCases

from memoryking.adapters.local_files import atomic
from memoryking.adapters.report_worker import Configuration, ReportWorker
from memoryking.domain.local import LocalError
from memoryking.reporting.smtp import submit as smtp_submit


class ConfigurationTests(unittest.TestCase):
    def test_closed_private_configuration_and_no_implicit_sender(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "report.json"
            value = {
                "version": 1,
                "enabled": False,
                "tenant": "main",
                "project": "p",
                "credential_file": None,
                "mail_secret_file": None,
                "sender_enabled": False,
                "poll_seconds": 1,
            }
            atomic(path, json.dumps(value).encode())
            config = Configuration.from_file(path)
            self.assertFalse(config.enabled)
            self.assertIsNone(config.mail)
            worker = ReportWorker(config, lambda: self.fail("disabled worker opened a store"))
            worker.start()
            self.assertEqual(worker.tick(), {"state": "disabled"})
            worker.stop()
            for change in (
                {"extra": True},
                {"enabled": True},
                {"sender_enabled": True},
                {"poll_seconds": 0},
            ):
                atomic(path, json.dumps({**value, **change}).encode())
                with self.assertRaises(LocalError):
                    Configuration.from_file(path)
            atomic(path, json.dumps(value).encode())
            path.chmod(0o644)
            with self.assertRaises(LocalError):
                Configuration.from_file(path)

    def test_secret_paths_are_explicit_private_and_outside_renderer_roots(self):
        import sys

        from memoryking.reporting.sandbox import validate_protected_paths

        with self.assertRaises(LocalError) as unsafe:
            validate_protected_paths([Path(sys.prefix) / "mail-secret.json"])
        self.assertEqual(unsafe.exception.code, "unsafe_renderer_layout")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, secret, token = root / "worker.json", root / "mail.json", root / "credential"
            atomic(token, b"synthetic-worker-credential")
            atomic(
                secret,
                json.dumps(
                    {
                        "version": 1,
                        "destination": "fixture",
                        "GMAIL_FROM": "s@example.invalid",
                        "GMAIL_APP_KEY": "fixture-password",
                        "KINDLE_TO": "k@example.invalid",
                    }
                ).encode(),
            )
            value = {
                "version": 1,
                "enabled": True,
                "tenant": "main",
                "project": "p",
                "credential_file": str(token),
                "mail_secret_file": str(secret),
                "sender_enabled": False,
                "poll_seconds": 1,
            }
            atomic(path, json.dumps(value).encode())
            config = Configuration.from_file(path)
            self.assertFalse(config.mail.enabled)
            self.assertEqual(
                set(config.protected), {path.resolve(), secret.resolve(), token.resolve()}
            )
            self.assertNotIn("fixture-password", repr(config))
            self.assertNotIn("synthetic-worker-credential", repr(config))
            token.unlink()
            token.symlink_to(secret)
            with self.assertRaises(LocalError):
                Configuration.from_file(path)


class WorkerCases:
    command = ReportCases.command
    policy = DeliveryCases.policy
    source = ReportCases.source
    upload = ReportCases.upload
    submitted = ReportCases.submitted
    build = ReportCases.build
    qa = ReportCases.qa
    publish = DeliveryCases.publish

    def worker(self, *, enabled=True, mail=False, credential=None):
        tenant = self.store._decode(self.store.namespace(self.owner, "p"), "namespace")["tenant"]
        config = Configuration(
            enabled,
            tenant,
            "p",
            credential or self.owner,
            (),
            self.store.report_mail if mail else None,
            1,
        )
        return ReportWorker(config, lambda: nullcontext(self.store))

    def test_background_build_stops_cleanly_without_qa_or_email(self):
        self.submitted()
        self.command("build", {"report": "phase", "revision": 1})
        worker = self.worker()
        worker.start()
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                status = self.command("status", {"report": "phase", "revision": 1})
                if status["state"] == "built":
                    break
                time.sleep(0.02)
            self.assertEqual(status["state"], "built")
            self.assertIsNone(status["qa"])
            self.assertEqual(
                self.store.db.execute("SELECT COUNT(*) FROM report_deliveries").fetchone()[0], 0
            )
        finally:
            worker.stop()
        self.assertFalse(worker.thread.is_alive())

    def test_start_rechecks_scope_and_worker_capability(self):
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        worker = self.worker(credential=reader)
        with self.assertRaises(LocalError):
            worker.start()
        worker = self.worker()
        worker.configuration = replace(worker.configuration, tenant="wrong")
        with self.assertRaises(LocalError) as denied:
            worker.start()
        self.assertEqual(denied.exception.code, "invalid_report_worker_scope")

    def test_revoked_first_build_does_not_starve_permitted_build(self):
        args = self.submitted()
        self.command("build", {"report": "phase", "revision": 1})
        self.source("evidence", b'{"result":"synthetic"}', enabled=False, version=1)
        replacement = self.source("second-evidence", b'{"result":"synthetic"}')
        args["dependencies"] = [args["dependencies"][0], replacement]
        from test_report_document import phase_source

        for name, raw in (
            ("source.md", phase_source().encode()),
            ("readme.md", b"# Verified fixture README"),
            ("evidence.json", b'{"result":"synthetic"}'),
        ):
            self.upload(name, raw)
        self.command("submit", {**args, "report": "second"})
        self.command("build", {"report": "second", "revision": 1})
        self.assertEqual(self.worker().tick()["state"], "built")
        self.assertEqual(
            self.command("status", {"report": "second", "revision": 1})["state"], "built"
        )
        self.assertEqual(
            self.store.db.execute(
                "SELECT state FROM report_revisions WHERE report='phase'"
            ).fetchone()[0],
            "draft",
        )

    def test_sender_disabled_then_fake_acceptance_is_not_repeated(self):
        self.publish()
        client = FakeSMTP()
        with patch(
            "memoryking.adapters.report_delivery.submit",
            lambda *args, **kwargs: smtp_submit(*args, **kwargs, smtp_factory=client.factory),
        ):
            self.worker().tick()
            self.assertEqual(client.events, [])
            worker = self.worker(mail=True)
            self.assertEqual(worker.tick()["state"], "smtp_accepted")
            events = list(client.events)
            worker.tick()
            worker.tick()
            self.assertEqual(client.events, events)

    def test_unknown_is_not_retried_by_future_ticks(self):
        self.publish()
        client = FakeSMTP(fail="reply")
        worker = self.worker(mail=True)
        with patch(
            "memoryking.adapters.report_delivery.submit",
            lambda *args, **kwargs: smtp_submit(*args, **kwargs, smtp_factory=client.factory),
        ):
            self.assertEqual(worker.tick()["state"], "unknown")
            events = list(client.events)
            worker.tick()
            worker.tick()
            self.assertEqual(client.events, events)


SELECTED = platform.system() == "Darwin" or (
    platform.system() == "Linux" and os.environ.get("MEMORYKING_REQUIRE_REPORT_SANDBOX") == "1"
)


@unittest.skipUnless(SELECTED, "native renderer not selected")
class LocalWorker(WorkerCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown

    @contextmanager
    def actor(self, credential):
        yield self.store


@unittest.skipUnless(SELECTED, "native renderer not selected")
class NativeWorker(WorkerCases, NativeFixture):
    pass
