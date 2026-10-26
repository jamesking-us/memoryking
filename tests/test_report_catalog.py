"""Both-store current catalog/figure visibility, cursor fencing and bounded data."""

import base64
import hashlib
import io
import json
import unittest
from contextlib import contextmanager

import test_knowledge as knowledge
from PIL import Image
from test_postgres_parity import NativeFixture
from test_report_document import phase_source
from test_report_store import ReportCases
from test_report_worker import SELECTED

from memoryking.adapters.report_build import build_one
from memoryking.domain.local import LocalError


class CatalogCases:
    command = ReportCases.command
    source = ReportCases.source
    upload = ReportCases.upload

    def publish(self, report, readers, *, image=False):
        self.command(
            "policy",
            {
                "expected_version": 0,
                "policy": {
                    "version": 1,
                    "project": "p",
                    "program": report,
                    "template": "phase-report",
                    "channel": "app",
                    "destination": None,
                    "recipient_principal": None,
                    "require_visual_qa": True,
                    "require_evidence": True,
                    "classification": "internal",
                    "readers": readers,
                },
            },
        )
        files = {"readme.md": b"# Synthetic README", "evidence.json": b'{"scope":"synthetic"}'}
        extra = ""
        if image:
            output = io.BytesIO()
            Image.new("RGB", (8, 8), "blue").save(output, format="PNG")
            files["diagram.png"] = output.getvalue()
            extra = "\n\n![Synthetic blue square](diagram.png)\n"
        dependencies = [
            self.source(report + "-" + name.split(".")[0], raw, readers)
            for name, raw in files.items()
        ]
        files["source.md"] = phase_source(extra).encode()
        for name, raw in files.items():
            self.upload(name, raw)
        self.command(
            "submit",
            {
                "report": report,
                "expected_revision": 0,
                "upload": "inputs",
                "program": report,
                "phase": "S01",
                "task": None,
                "title": "Synthetic phase",
                "technical_status": "partial",
                "classification": "internal",
                "dependencies": dependencies,
            },
        )
        self.command("build", {"report": report, "revision": 1})
        build_one(self.store, self.owner, "p")
        status = self.command("status", {"report": report, "revision": 1})
        artifacts = {a["name"]: a["sha256"] for a in status["artifacts"]}
        body = json.loads(
            self.store.db.execute(
                "SELECT body FROM report_revisions WHERE report=?", (report,)
            ).fetchone()[0]
        )
        self.command(
            "qa",
            {
                "report": report,
                "revision": 1,
                "attestation": {
                    "version": 1,
                    "report": report,
                    "revision": 1,
                    **{k: status[k] for k in ("source_sha256", "readme_sha256", "evidence_sha256")},
                    "pdf_sha256": artifacts["report.pdf"],
                    "epub_sha256": artifacts["report.epub"],
                    "reader_sha256": artifacts["reader.json"],
                    "pdf_pages_reviewed": body["pdf_pages"],
                    "pdf_reviewed": True,
                    "epub_reviewed": True,
                    "method": "Synthetic fixture, not human or actual phase QA",
                },
            },
        )
        self.command("publish", {"report": report, "revision": 1})
        return files

    def test_catalog_cursor_contains_only_visible_identity_and_revocation_fences_it(self):
        phone = self.store.grant(self.owner, "phone", "p", ["read"])
        self.publish("aaa", ["owner", "phone"])
        self.publish("private-title", ["owner"])
        self.publish("zzz", ["owner", "phone"])
        first = self.command("list", {"limit": 1, "cursor": None}, phone)
        self.assertEqual([r["report"] for r in first["reports"]], ["aaa"])
        cursor = first["next_cursor"]
        decoded = self.store._decode(cursor, "report_catalog")
        self.assertEqual(decoded["after"], "aaa")
        self.assertNotIn("private-title", json.dumps(first))
        second = self.command("list", {"limit": 1, "cursor": cursor}, phone)
        self.assertEqual([r["report"] for r in second["reports"]], ["zzz"])
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(second["reports"][0]["technical_status"], "partial")
        self.assertEqual(second["reports"][0]["review_status"], "pending")
        with self.assertRaises(LocalError):
            self.command("list", {"limit": 1, "cursor": cursor})
        self.source("aaa-evidence", b'{"scope":"synthetic"}', ["owner"], version=1)
        with self.assertRaises(LocalError):
            self.command("list", {"limit": 1, "cursor": cursor}, phone)
        current = self.command("list", {"limit": 50, "cursor": None}, phone)
        self.assertEqual([r["report"] for r in current["reports"]], ["zzz"])

    def test_image_read_requires_whole_report_authority_and_is_not_raw_export(self):
        phone = self.store.grant(self.owner, "phone", "p", ["read"])
        files = self.publish("figures", ["owner", "phone"], image=True)
        args = {"report": "figures", "revision": 1, "name": "diagram.png", "offset": 0}
        result = self.command("image", args, phone)
        self.assertEqual(base64.b64decode(result["data_base64"]), files["diagram.png"])
        self.assertEqual(result["sha256"], hashlib.sha256(files["diagram.png"]).hexdigest())
        for name in ("source.md", "report.pdf", "../diagram.png", "missing.png"):
            with self.assertRaises(LocalError):
                self.command("image", {**args, "name": name}, phone)
        self.source("figures-evidence", files["evidence.json"], ["owner"], version=1)
        with self.assertRaises(LocalError):
            self.command("image", args, phone)


@unittest.skipUnless(SELECTED, "native renderer not selected")
class LocalCatalog(CatalogCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown

    @contextmanager
    def actor(self, credential):
        yield self.store


@unittest.skipUnless(SELECTED, "native renderer not selected")
class NativeCatalog(CatalogCases, NativeFixture):
    pass
