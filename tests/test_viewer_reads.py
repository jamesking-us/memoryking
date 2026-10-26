"""Actual canonical viewer reads on both stores; HTTP/session issuance is separate."""

import json
import unittest
from dataclasses import replace

import test_knowledge as knowledge
from test_postgres_parity import NativeFixture
from test_viewer_contracts import config
from test_viewer_identity import ViewerCases
from test_working import checkpoint

from memoryking.adapters.viewer_reads import ViewerReads, ViewerSession
from memoryking.domain.local import LocalError


class ReadCases:
    command = ViewerCases.command
    policy = ViewerCases.policy
    bind = ViewerCases.bind
    grant_viewer = ViewerCases.grant_viewer
    subject = ViewerCases.subject
    actor_store = ViewerCases.actor_store
    body = knowledge.KnowledgeTests.body
    propose = knowledge.KnowledgeTests.propose
    review = knowledge.KnowledgeTests.review
    run_k = knowledge.KnowledgeTests.run_k

    def setup_viewer(self):
        self.bind()
        self.grant_viewer()
        subject = self.subject()
        self.settings = {**config(), "enabled": True, "tenant": subject.tenant}
        self.session = ViewerSession(
            subject, "synthetic-session", "synthetic-csrf", self.store.clock.utc_ms() + 60000
        )

    def read(self, route, project="p", params=None, session=None, raw=False):
        session = session or self.session
        with self.actor_store(session.subject.credential) as store:
            result = ViewerReads(store, self.settings).read(session, route, project, params or {})
            if raw:
                return result
            output = []
            result.emit(output.append)
            return json.loads(output[0])

    def test_current_session_and_project_intersection(self):
        self.setup_viewer()
        result = self.read("session", None)
        self.assertEqual(result["data"]["projects"], ["p"])
        self.assertNotIn("credential", json.dumps(result))
        self.assertLessEqual(
            result["binding"]["fresh_until_ms"] - result["binding"]["evaluated_ms"], 15000
        )
        for project in ("hidden", "absent"):
            with self.assertRaises(LocalError):
                self.read("overview", project)
        self.policy("revoke", {"principal": "phone", "project": "p", "expected_version": 2})
        with self.assertRaisesRegex(LocalError, "context_changed"):
            self.read("session", None)

    def test_memory_private_absent_search_and_session_handles(self):
        self.setup_viewer()
        result = self.read("memories")
        self.assertEqual(len(result["data"]["memories"]), 1)
        memory = result["data"]["memories"][0]
        detail = self.read("memory", params={"handle": memory["handle"]})
        self.assertEqual(detail["data"]["memories"][0]["text"], self.raw)
        with self.assertRaisesRegex(LocalError, "unavailable"):
            self.read(
                "memory",
                params={"handle": memory["handle"]},
                session=replace(self.session, identity="other"),
            )
        self.assertEqual(self.read("memories", params={"q": "absent"})["data"]["memories"], [])
        for handle in ("absent", "../../owner.credential"):
            with self.assertRaisesRegex(LocalError, "unavailable"):
                self.read("memory", params={"handle": handle})

    def test_cursor_binds_session_query_frontier_and_scope(self):
        self.setup_viewer()
        for i in range(3):
            self.store.capture(
                self.owner,
                self.ns,
                f"extra-{i}",
                {
                    "version": 1,
                    "project": "p",
                    "source": "test",
                    "source_event": f"extra-{i}",
                    "text": "visible",
                    "readers": ["phone", "owner"],
                    "classification": "internal",
                    "observed_at_ms": 1,
                },
            )
        first = self.read("memories", params={"limit": 1})
        cursor = first["next_cursor"]
        self.assertIsNotNone(cursor)
        second = self.read("memories", params={"limit": 1, "cursor": cursor})
        self.assertNotEqual(
            first["data"]["memories"][0]["text"], second["data"]["memories"][0]["text"]
        )
        for params, session in [
            ({"limit": 2, "cursor": cursor}, self.session),
            ({"limit": 1, "q": "visible", "cursor": cursor}, self.session),
            ({"limit": 1, "cursor": cursor}, replace(self.session, identity="other")),
        ]:
            with self.assertRaisesRegex(LocalError, "invalid_cursor"):
                self.read("memories", params=params, session=session)
        self.store.capture(
            self.owner,
            self.ns,
            "new-frontier",
            {
                "version": 1,
                "project": "p",
                "source": "test",
                "source_event": "new-frontier",
                "text": "hidden",
                "readers": ["owner"],
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )
        with self.assertRaisesRegex(LocalError, "invalid_cursor"):
            self.read("memories", params={"limit": 1, "cursor": cursor})

    def test_no_snapshot_or_hidden_task_fallback(self):
        self.setup_viewer()
        self.assertEqual(self.read("tasks")["data"]["tasks"], [])
        for task in ("missing", "private-task"):
            with self.assertRaisesRegex(LocalError, "unavailable"):
                self.read("task", params={"task": task})
        # Use the actual checkpoint operation; the browser has no write path.
        body = checkpoint()
        body["references"] = []
        body["acceptance"] = []
        body["verification"] = []
        body["repository"]["artifacts"] = []
        from memoryking.adapters.working import Working

        Working(self.store).execute(
            self.owner,
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": "save",
                "key": "viewer-task",
                "args": {
                    "project": "p",
                    "task": "visible",
                    "run": None,
                    "expected_revision": 0,
                    "readers": ["owner", "phone"],
                    "checkpoint": body,
                },
            },
        )
        result = self.read("tasks")
        self.assertEqual([t["id"] for t in result["data"]["tasks"]], ["visible"])
        self.assertNotIn("repository", json.dumps(result))
        self.assertEqual(
            self.read("task", params={"task": "visible"})["data"]["tasks"], result["data"]["tasks"]
        )
        graph = self.read("graph")["data"]
        self.assertEqual(len(graph["nodes"]), 1)

    def test_unknown_constraint_blocks_without_hidden_identity(self):
        self.setup_viewer()
        proposed = self.propose(
            self.body(
                kind="constraint",
                selector=knowledge.sel(knowledge.eq("environment", "production")),
                authority="project",
                rule_key="safety",
                effect="require",
                rule_value="review",
            )
        )
        self.review(proposed, "accept")
        self.session = replace(self.session, subject=self.subject())
        with self.assertRaisesRegex(LocalError, "unavailable"):
            self.read("memories")

    def test_deadline_and_expired_session(self):
        self.setup_viewer()
        from test_local_store import FakeClock

        now = self.store.clock.utc_ms()
        clock = FakeClock()
        clock.utc = now
        self.store.clock = clock
        result = self.read("memories", raw=True)
        clock.advance(5000)
        with self.assertRaises(LocalError):
            result.emit(lambda _: self.fail("late protected response"))
        with self.assertRaisesRegex(LocalError, "unauthenticated"):
            self.read("session", None, session=replace(self.session, expires_ms=now))


class LocalViewerReads(ReadCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown


class PostgresViewerReads(ReadCases, NativeFixture):
    pass
