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
        body["acceptance"] = [
            {
                "id": "proof",
                "text": "Recorded result",
                "state": "passed",
                "evidence": [{"kind": "observation", "id": self.obs["observation"]}],
                "decision": None,
            }
        ]
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
        detail = self.read("task", params={"task": "visible"})["data"]["tasks"][0]
        self.assertEqual(detail["id"], "visible")
        self.assertFalse(detail["text_truncated"])
        self.assertEqual(detail["progress"]["passed"], 1)
        reference = detail["criteria"][0]["evidence"][0]
        self.assertEqual(
            self.read("memory", params={"handle": reference["handle"]})["data"]["memories"][0][
                "text"
            ],
            self.raw,
        )
        graph = self.read("graph")["data"]
        self.assertEqual(len(graph["nodes"]), 3)
        self.assertEqual({e["kind"] for e in graph["edges"]}, {"declares", "uses"})
        self.store.purge(self.owner, self.obs["observation"])
        self.session = replace(self.session, subject=self.subject())
        with self.assertRaisesRegex(LocalError, "unavailable"):
            self.read("task", params={"task": "visible"})

    def test_knowledge_graph_current_links_targets_and_bounded_private_sample(self):
        self.setup_viewer()
        accepted = self.review(self.propose(self.body("Visible accepted claim")))
        self.propose(self.body("Unreviewed claim must not appear"), key="unreviewed")
        self.review(self.propose(self.body("Owner-only claim", readers=["owner"]), key="private"))
        for i in range(55):
            self.store.capture(
                self.owner,
                self.ns,
                f"graph-{i}",
                {
                    "version": 1,
                    "project": "p",
                    "source": "test",
                    "source_event": f"graph-{i}",
                    "text": "Owner-only sentinel" if i == 0 else f"Visible observation {i}",
                    "readers": ["owner"] if i == 0 else ["owner", "phone"],
                    "classification": "internal",
                    "observed_at_ms": 1,
                },
            )
        self.session = replace(self.session, subject=self.subject())
        response = self.read("graph")
        graph = response["data"]
        self.assertTrue(response["truncated"])
        self.assertLessEqual(len(graph["nodes"]), 100)
        self.assertLessEqual(len(graph["edges"]), 150)
        self.assertNotIn("Owner-only", json.dumps(graph))
        self.assertNotIn("Unreviewed", json.dumps(graph))
        self.assertNotIn(accepted["revision"], json.dumps(graph))
        claim = next(n for n in graph["nodes"] if n["kind"] == "claim")
        observation = next(n for n in graph["nodes"] if n["label"] == self.raw)
        self.assertIn(
            {"source": claim["id"], "target": observation["id"], "kind": "cites"}, graph["edges"]
        )
        self.assertEqual(
            self.read("memory", params={"handle": claim["handle"]})["data"]["memories"][0]["text"],
            "Visible accepted claim",
        )
        with self.assertRaisesRegex(LocalError, "unavailable"):
            self.read(
                "memory",
                params={"handle": claim["handle"]},
                session=replace(self.session, identity="other"),
            )
        self.settings["limits"].update(graph_nodes=1, graph_edges=1)
        tiny = self.read("graph")
        self.assertEqual(len(tiny["data"]["nodes"]), 1)
        self.assertEqual(tiny["data"]["edges"], [])
        self.assertTrue(tiny["truncated"])
        self.store.purge(self.owner, self.obs["observation"])
        self.session = replace(self.session, subject=self.subject())
        with self.assertRaisesRegex(LocalError, "unavailable"):
            self.read("memory", params={"handle": claim["handle"]})
        with self.assertRaisesRegex(LocalError, "unavailable"):
            self.read("graph")  # Required knowledge reconciliation fails closed after purge.

    def test_plan_graph_includes_recorded_links_and_late_evidence(self):
        from test_plans import plan

        from memoryking.adapters.working import Working

        self.setup_viewer()
        for i in range(70):
            self.store.capture(
                self.owner,
                self.ns,
                f"backlog-{i}",
                {
                    "version": 1,
                    "project": "p",
                    "source": "test",
                    "source_event": f"backlog-{i}",
                    "text": f"Unlinked backlog {i}",
                    "readers": ["owner", "phone"],
                    "classification": "internal",
                    "observed_at_ms": 1,
                },
            )
        evidence = self.store.capture(
            self.owner,
            self.ns,
            "late-proof",
            {
                "version": 1,
                "project": "p",
                "source": "test",
                "source_event": "late-proof",
                "text": "Actual late plan evidence",
                "readers": ["owner", "phone"],
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )
        value = plan()
        value["steps"][0]["evidence"] = [{"kind": "observation", "id": evidence["observation"]}]
        value["lessons"] = [{"kind": "observation", "id": self.obs["observation"]}]
        for task, readers in [("checkout", ["owner", "phone"]), ("secret-plan", ["owner"])]:
            Working(self.store).execute(
                self.owner,
                {
                    "version": 1,
                    "incarnation": self.store.incarnation,
                    "action": "plan_save",
                    "key": task,
                    "args": {
                        "project": "p",
                        "task": task,
                        "expected_revision": 0,
                        "readers": readers,
                        "plan": value,
                    },
                },
            )
        graph = self.read("graph")["data"]
        by_id = {n["id"]: n for n in graph["nodes"]}
        self.assertNotIn("secret-plan", json.dumps(graph))
        self.assertEqual(by_id["plan:checkout"]["kind"], "plan")
        self.assertEqual(by_id["plan:checkout:inspect"]["phase"], "design")
        self.assertIn(
            {
                "source": "plan:checkout:test",
                "target": "plan:checkout:inspect",
                "kind": "depends_on",
            },
            graph["edges"],
        )
        proof = next(n for n in graph["nodes"] if n["label"] == "Actual late plan evidence")
        self.assertIn(
            {"source": "plan:checkout:inspect", "target": proof["id"], "kind": "uses"},
            graph["edges"],
        )
        self.assertEqual(
            self.read("memory", params={"handle": proof["handle"]})["data"]["memories"][0]["text"],
            proof["label"],
        )
        self.assertTrue(all(e["source"] in by_id and e["target"] in by_id for e in graph["edges"]))
        self.settings["limits"].update(graph_nodes=5, graph_edges=1)
        small = self.read("graph")
        self.assertLessEqual(len(small["data"]["nodes"]), 5)
        self.assertLessEqual(len(small["data"]["edges"]), 1)
        self.assertTrue(small["truncated"])
        self.policy("revoke", {"principal": "phone", "project": "p", "expected_version": 2})
        with self.assertRaisesRegex(LocalError, "context_changed"):
            self.read("graph")

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

    def test_persisted_instant_grant_expiry_and_display_lease(self):
        self.setup_viewer()
        from test_local_store import FakeClock

        from memoryking.contracts.canonical import canonical_hash

        clock = FakeClock()
        clock.utc = self.store.last_utc + 10000
        self.store.clock = clock
        self.grant_viewer(version=2, expires=clock.utc + 1000)
        self.session = replace(self.session, subject=self.subject(), expires_ms=clock.utc + 60000)
        evaluated = clock.utc

        def stall(point):
            if point == "after_validation":
                clock.advance(2000)

        self.store.fault = stall
        result = self.read("memories")
        self.store.fault = lambda _: None
        self.assertEqual(result["binding"]["evaluated_ms"], evaluated)
        self.assertEqual(result["binding"]["fresh_until_ms"], evaluated + 1000)
        # Canonical validity at the persisted evaluation instant permits completion;
        # a client must discard this now-expired display lease.
        receipt = self.store.db.execute(
            "SELECT evaluated FROM receipts WHERE tenant=? AND response_hash=?",
            (self.tenant, canonical_hash(result)),
        ).fetchone()
        self.assertEqual(receipt[0], evaluated)
        with self.assertRaises(LocalError):
            self.read("memories")

    def test_constraint_activation_and_verification_expiry_cut_lease(self):
        self.setup_viewer()
        from test_local_store import FakeClock

        clock = FakeClock()
        clock.utc = self.store.last_utc + 10000
        self.store.clock = clock
        accepted = self.review(self.propose())
        self.run_k(
            "attest",
            {
                "project": "p",
                "revision": accepted["revision"],
                "expires_ms": clock.utc + 1000,
                "statement": "verified",
            },
        )
        self.session = replace(self.session, subject=self.subject(), expires_ms=clock.utc + 60000)
        self.assertEqual(self.read("memories")["binding"]["fresh_until_ms"], clock.utc + 1000)
        clock.advance(1001)
        self.assertGreater(self.read("memories")["binding"]["fresh_until_ms"], clock.utc)
        boundary = clock.utc + 1000
        constraint = self.propose(
            self.body(
                kind="constraint",
                selector=knowledge.sel(knowledge.eq("environment", "production")),
                authority="project",
                rule_key="safety",
                effect="require",
                rule_value="review",
                valid_start=boundary,
                valid_end=boundary + 1000,
            ),
            key="later-constraint",
        )
        self.review(constraint)
        self.session = replace(self.session, subject=self.subject())
        self.assertEqual(self.read("memories")["binding"]["fresh_until_ms"], boundary)
        clock.advance(1000)
        with self.assertRaisesRegex(LocalError, "unavailable"):
            self.read("memories")
        clock.advance(1000)
        self.assertTrue(self.read("memories")["data"]["memories"])


class LocalViewerReads(ReadCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown


class PostgresViewerReads(ReadCases, NativeFixture):
    pass
