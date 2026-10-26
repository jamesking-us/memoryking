"""Both stores: real report rendering, current ACLs, durable bytes and failure fencing."""

import base64
import hashlib
import json
import platform
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import patch

import psycopg
import test_knowledge as knowledge
from recovery_helpers import backup_fixture, restore_fixture
from test_postgres_parity import NativeFixture
from test_report_document import phase_source

from memoryking.adapters.report_build import build_one
from memoryking.adapters.report_store import Reports
from memoryking.domain.local import LocalError
from memoryking.domain.reporting import CHUNK_LIMIT


class ReportCases:
    @unittest.skipUnless(platform.system() == "Darwin", "native macOS renderer")
    def test_renderer_releases_request_gate_and_duplicate_worker_is_excluded(self):
        self.submitted()
        self.command("build", {"report": "phase", "revision": 1})
        entered, proceed = threading.Event(), threading.Event()
        from memoryking.adapters.report_build import render as actual_render

        def held(stage, **kwargs):
            entered.set()
            if not proceed.wait(5):
                raise AssertionError("test controller did not release renderer")
            actual_render(stage, **kwargs)

        with patch("memoryking.adapters.report_build.render", held), ThreadPoolExecutor(1) as pool:
            future = pool.submit(build_one, self.store, self.owner, "p")
            self.assertTrue(entered.wait(3))
            try:
                self.assertEqual(
                    self.command("status", {"report": "phase", "revision": 1})["state"], "building"
                )
                with self.assertRaises(LocalError) as duplicate:
                    build_one(self.store, self.owner, "p")
                self.assertEqual(duplicate.exception.code, "owner_busy_or_unsafe")
            finally:
                proceed.set()
            self.assertEqual(future.result(timeout=10)["state"], "built")

    @unittest.skipUnless(platform.system() == "Darwin", "native macOS renderer")
    def test_exact_qa_and_corrupted_artifact_block_publication(self):
        self.submitted()
        self.build()
        qa = self.qa()
        self.assertEqual(qa["source_sha256"], hashlib.sha256(phase_source().encode()).hexdigest())
        row = self.store.db.execute(
            "SELECT tenant,blob FROM report_files WHERE name='report.epub'"
        ).fetchone()
        from memoryking.adapters.local_files import atomic

        atomic(self.data / "blobs" / row["tenant"] / row["blob"], b"corrupt")
        with self.assertRaises(LocalError) as failed:
            self.command("publish", {"report": "phase", "revision": 1})
        self.assertEqual(failed.exception.code, "blob_corrupt")
        self.assertEqual(
            self.store.db.execute("SELECT state FROM report_revisions").fetchone()[0], "qa_verified"
        )

    def test_chunked_input_conflict_and_scoped_discard(self):
        raw = phase_source("\n\n" + "Long Unicode text 🦣. " * 5000).encode()
        self.upload("source.md", raw)
        chunks = self.store.db.execute("SELECT COUNT(*) FROM report_uploads").fetchone()[0]
        self.assertGreater(chunks, 1)
        with self.assertRaises(LocalError) as conflict:
            self.upload("source.md", phase_source("changed").encode())
        self.assertEqual(conflict.exception.code, "report_input_conflict")
        self.command("discard_upload", {"upload": "inputs"})
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM report_uploads").fetchone()[0], 0
        )

    def test_restore_keeps_current_source_policy_and_retires_report_history(self):
        self.submitted()
        previous_history = self.store.incarnation
        archive = backup_fixture(self)
        self.source("evidence", b'{"result":"synthetic"}', enabled=False, version=1)
        restore_fixture(self, archive)
        self.assertNotEqual(self.store.incarnation, previous_history)
        source = self.store.db.execute(
            "SELECT body FROM report_sources WHERE id='evidence'"
        ).fetchone()[0]
        self.assertFalse(json.loads(source)["enabled"])
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM report_files").fetchone()[0], 3
        )
        with self.assertRaises(LocalError) as stale:
            self.command("status", {"report": "phase", "revision": 1})
        self.assertEqual(stale.exception.code, "history_changed")

    def command(self, action, args, credential=None, key=None):
        self.n += 1
        with self.actor(credential) as store:
            result = []
            Reports(store).execute(
                credential or self.owner,
                {
                    "version": 1,
                    "incarnation": store.incarnation,
                    "key": key or f"report_{self.n}",
                    "action": action,
                    "args": {"project": "p", **args},
                },
            ).emit(result.append)
            return json.loads(result[0])

    def policy(self, readers=None, version=0):
        return self.command(
            "policy",
            {
                "expected_version": version,
                "policy": {
                    "version": 1,
                    "project": "p",
                    "program": "synthetic",
                    "template": "phase-report",
                    "channel": "app",
                    "destination": None,
                    "recipient_principal": None,
                    "require_visual_qa": True,
                    "require_evidence": True,
                    "classification": "restricted",
                    "readers": readers or ["owner"],
                },
            },
        )

    def source(self, name, raw, readers=None, version=0, enabled=True):
        self.command(
            "bind_source",
            {
                "expected_version": version,
                "binding": {
                    "id": name,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "readers": readers or ["owner"],
                    "classification": "internal",
                    "enabled": enabled,
                    "starts": None,
                    "expires": None,
                },
            },
        )
        return {"kind": "external_source", "id": name, "sha256": hashlib.sha256(raw).hexdigest()}

    def upload(self, name, raw, upload="inputs", credential=None):
        for index in range(0, len(raw), CHUNK_LIMIT):
            self.command(
                "upload",
                {
                    "upload": upload,
                    "name": name,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "bytes": len(raw),
                    "part": index // CHUNK_LIMIT,
                    "parts": (len(raw) + CHUNK_LIMIT - 1) // CHUNK_LIMIT,
                    "data_base64": base64.b64encode(raw[index : index + CHUNK_LIMIT]).decode(),
                },
                credential,
            )

    def submitted(self, readers=None, extra="", report="phase", revision=0):
        if revision == 0:
            self.policy(readers)
        readme, evidence = b"# Verified fixture README", b'{"result":"synthetic"}'
        deps = [
            self.source("readme", readme, readers, version=revision),
            self.source("evidence", evidence, readers, version=revision),
        ]
        for name, raw in [
            ("source.md", phase_source(extra).encode()),
            ("readme.md", readme),
            ("evidence.json", evidence),
        ]:
            self.upload(name, raw)
        args = {
            "report": report,
            "expected_revision": revision,
            "upload": "inputs",
            "program": "synthetic",
            "phase": "S01",
            "task": None,
            "title": "Synthetic phase",
            "technical_status": "complete",
            "classification": "internal",
            "dependencies": deps,
        }
        result = self.command("submit", args, key=f"submit-{report}-{revision}")
        self.assertEqual(result["revision"], revision + 1)
        return args

    def build(self):
        self.command("build", {"report": "phase", "revision": 1})
        return build_one(self.store, self.owner, "p")

    def qa(self):
        status = self.command("status", {"report": "phase", "revision": 1})
        artifacts = {a["name"]: a["sha256"] for a in status["artifacts"]}
        body = json.loads(
            self.store.db.execute(
                "SELECT body FROM report_revisions WHERE report='phase'"
            ).fetchone()[0]
        )
        attestation = {
            "version": 1,
            "report": "phase",
            "revision": 1,
            **{key: status[key] for key in ["source_sha256", "readme_sha256", "evidence_sha256"]},
            "pdf_sha256": artifacts["report.pdf"],
            "epub_sha256": artifacts["report.epub"],
            "reader_sha256": artifacts["reader.json"],
            "pdf_pages_reviewed": body["pdf_pages"],
            "pdf_reviewed": True,
            "epub_reviewed": True,
            "method": "synthetic test attestation; not actual phase QA",
        }
        self.command("qa", {"report": "phase", "revision": 1, "attestation": attestation})
        return attestation

    def test_scoped_upload_immutable_submit_and_control_authority(self):
        self.submitted()
        author = self.store.grant(self.owner, "author", "p", ["read", "report_author"])
        with self.assertRaises(LocalError) as denied:
            self.command(
                "policy",
                {
                    "expected_version": 0,
                    "policy": {
                        "version": 1,
                        "project": "p",
                        "program": "evil",
                        "template": "phase-report",
                        "channel": "app",
                        "destination": None,
                        "recipient_principal": None,
                        "require_visual_qa": True,
                        "require_evidence": True,
                        "classification": "restricted",
                        "readers": ["author"],
                    },
                },
                author,
            )
        self.assertEqual(denied.exception.code, "denied")
        before = self.store.db.execute("SELECT body FROM report_revisions").fetchone()[0]
        self.upload("source.md", phase_source("Changed content").encode())
        self.assertEqual(
            self.store.db.execute("SELECT body FROM report_revisions").fetchone()[0], before
        )
        with self.assertRaises(LocalError):
            self.command("status", {"report": "phase", "revision": 1}, author)
        blobs = list(self.store.db.execute("SELECT blob FROM report_files"))
        self.store.gc()
        for row in blobs:
            tenant = self.store._decode(self.owner, "credential")["tenant"]
            self.assertTrue((self.data / "blobs" / tenant / row[0]).is_file())

    def test_unmapped_external_input_cannot_be_submitted(self):
        self.policy()
        for name, raw in [
            ("source.md", phase_source().encode()),
            ("readme.md", b"private"),
            ("evidence.json", b'{"private":true}'),
        ]:
            self.upload(name, raw)
        with self.assertRaises(LocalError) as denied:
            self.command(
                "submit",
                {
                    "report": "leak",
                    "expected_revision": 0,
                    "upload": "inputs",
                    "program": "synthetic",
                    "phase": "S01",
                    "task": None,
                    "title": "Synthetic phase",
                    "technical_status": "complete",
                    "classification": "internal",
                    "dependencies": [
                        {"kind": "external_source", "id": "unknown", "sha256": "a" * 64}
                    ],
                },
            )
        self.assertEqual(denied.exception.code, "report_dependency_unavailable")
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM report_revisions").fetchone()[0], 0
        )

    @unittest.skipUnless(platform.system() == "Darwin", "native macOS renderer")
    def test_real_bundle_qa_publish_reader_and_revocation(self):
        phone = self.store.grant(self.owner, "phone", "p", ["read"])
        self.submitted(["owner", "phone"])
        with self.assertRaises(LocalError):
            self.command("status", {"report": "phase", "revision": 1}, phone)
        with self.assertRaises(LocalError) as premature:
            self.command("publish", {"report": "phase", "revision": 1})
        self.assertEqual(premature.exception.code, "report_qa_required")
        self.assertEqual(self.build()["state"], "built")
        qa = self.qa()
        exported = self.command(
            "artifact", {"report": "phase", "revision": 1, "name": "report.pdf", "offset": 0}
        )
        self.assertTrue(base64.b64decode(exported["data_base64"]).startswith(b"%PDF-"))
        with self.assertRaises(LocalError):
            self.command(
                "artifact",
                {"report": "phase", "revision": 1, "name": "report.pdf", "offset": 0},
                phone,
            )
        self.command("publish", {"report": "phase", "revision": 1})
        status = self.command("status", {"report": "phase", "revision": 1}, phone)
        self.assertEqual(status["qa"]["pdf_sha256"], qa["pdf_sha256"])
        self.assertFalse(status["qa"]["human_approval"])
        self.assertEqual(status["review_status"], "pending")
        reader = self.command(
            "reader", {"report": "phase", "revision": 1, "page": 0, "cursor": None}, phone
        )
        self.assertLessEqual(len(json.dumps(reader).encode()), CHUNK_LIMIT)
        self.source("evidence", b'{"result":"synthetic"}', ["owner"], version=1)
        for action, args in [
            ("status", {"report": "phase", "revision": 1}),
            (
                "reader",
                {"report": "phase", "revision": 1, "page": 1, "cursor": reader["next_cursor"]},
            ),
        ]:
            with self.assertRaises(LocalError):
                self.command(action, args, phone)

    @unittest.skipUnless(platform.system() == "Darwin", "native macOS renderer")
    def test_manifest_failure_does_not_publish_partial_bundle(self):
        self.submitted()
        self.command("build", {"report": "phase", "revision": 1})

        def fault(name):
            if name == "report_before_manifest_commit":
                raise LocalError("injected_manifest_failure")

        self.store.fault = fault
        with self.assertRaises(LocalError) as failed:
            build_one(self.store, self.owner, "p")
        self.store.fault = lambda _: None
        self.assertEqual(failed.exception.code, "injected_manifest_failure")
        self.assertEqual(
            self.store.db.execute("SELECT state FROM report_revisions").fetchone()[0], "failed"
        )
        self.assertEqual(
            self.store.db.execute(
                "SELECT COUNT(*) FROM report_files WHERE kind<>'input'"
            ).fetchone()[0],
            0,
        )
        self.store.gc()

    @unittest.skipUnless(platform.system() == "Darwin", "native macOS renderer")
    def test_withdrawal_fences_worker_and_purge_erases_all_derived_refs(self):
        self.submitted()
        self.command("build", {"report": "phase", "revision": 1})
        from memoryking.adapters.report_build import render as actual_render

        def withdraw(stage, **kwargs):
            actual_render(stage, **kwargs)
            self.command("withdraw", {"report": "phase", "revision": 1})

        with (
            patch("memoryking.adapters.report_build.render", withdraw),
            self.assertRaises(LocalError) as fenced,
        ):
            build_one(self.store, self.owner, "p")
        self.assertEqual(fenced.exception.code, "report_worker_fenced")
        self.assertEqual(
            self.store.db.execute(
                "SELECT COUNT(*) FROM report_files WHERE kind='artifact'"
            ).fetchone()[0],
            0,
        )
        self.store.purge(self.owner, self.obs["observation"])
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM report_files").fetchone()[0], 0
        )
        self.assertIsNone(self.store.db.execute("SELECT body FROM report_revisions").fetchone()[0])


class LocalReports(ReportCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown

    @contextmanager
    def actor(self, credential):
        yield self.store


class NativeReports(ReportCases, NativeFixture):
    def test_report_tables_are_isolated_and_projection_worker_cannot_mutate(self):
        self.submitted()
        tables = (
            "report_sources",
            "report_policies",
            "report_control_retries",
            "report_revisions",
            "report_files",
            "report_retries",
        )
        with psycopg.connect(self.cluster.dsn("mk_runtime"), autocommit=True) as db:
            for table in tables:
                self.assertEqual(
                    db.execute(f"SELECT COUNT(*) FROM memoryking.{table}").fetchone()[0], 0
                )
                with db.transaction():
                    db.execute("SELECT set_config('memoryking.tenant',%s,true)", (self.tenant,))
                    self.assertGreater(
                        db.execute(f"SELECT COUNT(*) FROM memoryking.{table}").fetchone()[0], 0
                    )
                with db.transaction():
                    db.execute("SELECT set_config('memoryking.tenant','unrelated',true)")
                    self.assertEqual(
                        db.execute(f"SELECT COUNT(*) FROM memoryking.{table}").fetchone()[0], 0
                    )
        with psycopg.connect(self.cluster.dsn("mk_worker"), autocommit=True) as db:
            for table in (*tables, "report_uploads", "report_deliveries"):
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    db.execute(f"DELETE FROM memoryking.{table}")
