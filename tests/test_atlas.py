"""Atlas assertions shared by actual SQLite and native PostgreSQL services."""

import json
import unittest

import test_knowledge as knowledge
import test_retrieval as retrieval

from memoryking.adapters.retrieval import Retrieval
from memoryking.domain.local import LocalError


class AtlasCases:
    body = knowledge.KnowledgeTests.body
    propose = knowledge.KnowledgeTests.propose
    review = knowledge.KnowledgeTests.review
    fails = retrieval.RetrievalTests.fails
    clock = retrieval.RetrievalTests.clock

    def atlas(self, credential=None, **args):
        return self.run_r("atlas", {"project": "p", "bindings": [{}], **args}, credential)

    def accepted(self, **body):
        item = self.propose(self.body(**body))
        self.review(item)
        return item

    def test_graph_list_provenance_and_receipt_replay(self):
        claim = self.accepted()
        result = self.atlas()
        nodes = {n["id"]: n for n in result["nodes"]}
        self.assertIn(claim["revision"], nodes)
        self.assertEqual(set(nodes), {n["id"] for n in result["list"]})
        edge = result["edges"][0]
        self.assertEqual(edge["source"], self.obs["observation"])
        self.assertEqual(edge["target"], claim["revision"])
        self.assertEqual(edge["type"], "supports")
        self.assertEqual(edge["provenance"]["content_hash"], self.span["content_hash"])
        self.assertEqual(sum(result["counts"].values()), len(nodes))
        self.assertEqual(result["count_scope"], "returned_page_only")
        self.assertEqual(result["similarity"], "unavailable")
        replay = self.run_r("replay", {"project": "p", "receipt": result["receipt"]})
        self.assertEqual(result, replay)
        receipts = self.run_r("receipts", {"project": "p"})
        self.assertEqual(receipts["items"][0]["status"], "partial")

    def test_plan_steps_join_the_graph_with_their_evidence_and_prerequisites(self):
        from test_plans import plan

        from memoryking.adapters.working import Working

        body = plan()
        body["steps"][0].update(
            status="done", evidence=[{"kind": "observation", "id": self.obs["observation"]}]
        )
        output = []
        Working(self.store).execute(
            self.owner,
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": "plan_save",
                "key": "atlas-plan",
                "args": {
                    "project": "p",
                    "task": "checkout",
                    "expected_revision": 0,
                    "readers": ["owner"],
                    "plan": body,
                },
            },
        ).emit(output.append)
        checkpoint = json.loads(output[0])["checkpoint"]
        result = self.atlas()
        nodes = {n["id"]: n for n in result["nodes"]}
        self.assertEqual(nodes[checkpoint]["type"], "plan")
        self.assertEqual(nodes[checkpoint]["label"], "Reliable checkout")
        inspect, test = f"step:{checkpoint}:inspect", f"step:{checkpoint}:test"
        self.assertEqual(nodes[inspect]["status"], "done")
        self.assertEqual(nodes[test]["phase"], "verify")
        edges = {(e["source"], e["target"], e["type"]) for e in result["edges"]}
        self.assertIn((checkpoint, inspect, "declares"), edges)
        self.assertIn((test, inspect, "depends_on"), edges)
        self.assertIn((inspect, self.obs["observation"], "cites"), edges)
        self.assertEqual(result["counts"]["plan"], 1)
        self.assertEqual(result["counts"]["step"], 2)
        only = self.atlas(types=["step"])
        self.assertEqual({n["type"] for n in only["nodes"]}, {"step"})
        # Purging the proof removes the plan (its checkpoint depends on it), not the graph.
        self.store.purge(self.owner, self.obs["observation"])
        after = {n["id"] for n in self.atlas()["nodes"]}
        self.assertNotIn(checkpoint, after)
        self.assertNotIn(inspect, after)

    def test_citations_count_the_callers_briefings_but_not_atlas_pages(self):
        claim = self.accepted()
        first = self.atlas()
        nodes = {n["id"]: n for n in first["nodes"]}
        self.assertEqual(nodes[claim["revision"]]["citations"], 0)
        self.assertEqual(nodes[self.obs["observation"]]["citations"], 0)
        briefing = self.run_r("brief", retrieval.RetrievalTests.brief_args(self))
        self.assertIn(briefing["status"], {"complete", "partial"})
        self.atlas()  # looking is not citing
        second = self.atlas()
        nodes = {n["id"]: n for n in second["nodes"]}
        self.assertEqual(nodes[claim["revision"]]["citations"], 1)
        self.assertEqual(nodes[self.obs["observation"]]["citations"], 1)
        trail = Retrieval(self.store).list_citations(
            self.store._auth(self.owner, "read", "p", self.store.clock.utc_ms()),
            "p",
            claim["revision"],
            self.store.clock.utc_ms(),
        )
        self.assertEqual([t["receipt"] for t in trail], [briefing["receipt"]])
        self.assertEqual(trail[0]["dependency_kind"], "revision")
        self.assertEqual(trail[0]["status"], briefing["status"])
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        # Receipts are private to the principal that holds them.
        self.assertEqual({n["citations"] for n in self.atlas(reader)["nodes"]}, {0})
        self.fails(
            "invalid_request",
            lambda: Retrieval(self.store).list_citations(
                self.store._auth(self.owner, "read", "p", 0), "p", "bad id", 0
            ),
        )

    def test_reviewer_sees_proposed_and_quarantined_claims_but_a_reader_does_not(self):
        proposed = self.propose(self.body(text="proposed pear"), key="pear")
        quarantined = self.propose(self.body(text="quarantined quince"), key="quince")
        self.review(self.review(quarantined), decision="quarantine")
        nodes = {n["id"]: n for n in self.atlas()["nodes"]}
        self.assertEqual(nodes[proposed["revision"]]["state"], "proposed")
        self.assertFalse(nodes[proposed["revision"]]["quarantined"])
        self.assertEqual(nodes[quarantined["revision"]]["state"], "accepted")
        self.assertTrue(nodes[quarantined["revision"]]["quarantined"])
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        shown = json.dumps(self.atlas(reader))
        self.assertNotIn(proposed["revision"], shown)
        self.assertNotIn("quince", shown)

    def test_hidden_claim_has_no_node_edge_label_count_or_focus_signal(self):
        secret = self.accepted(text="restricted pineapple", readers=["owner"])
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        result = self.atlas(reader)
        self.assertNotIn(secret["revision"], json.dumps(result))
        self.assertNotIn("pineapple", json.dumps(result))
        self.assertEqual(result["counts"]["claim"], 0)
        self.assertEqual(result["edges"], [])
        hidden = self.atlas(reader, focus=[secret["revision"]])
        absent = self.atlas(reader, focus=["rev_missing"])
        for name in ("nodes", "edges", "counts", "list", "truncation", "next_cursor"):
            self.assertEqual(hidden[name], absent[name])

    def test_pages_are_bounded_bound_to_current_view_and_identity(self):
        self.accepted()
        first = self.atlas(max_nodes=1)
        self.assertEqual(len(first["nodes"]), 1)
        self.assertEqual(first["edges"], [])
        self.assertIn("edges_outside_page", first["truncation"])
        second = self.atlas(max_nodes=1, cursor=first["next_cursor"])
        self.assertFalse({n["id"] for n in first["nodes"]} & {n["id"] for n in second["nodes"]})
        self.assertIsNone(second["next_cursor"])
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        self.fails(
            "atlas_resync_required",
            lambda: self.atlas(reader, max_nodes=1, cursor=first["next_cursor"]),
        )
        self.fails(
            "atlas_resync_required", lambda: self.atlas(max_nodes=1, cursor=first["next_cursor"])
        )

    def test_expiry_changes_membership_and_invalidates_cursor(self):
        clock = self.clock()
        claim = self.accepted(valid_end=clock.utc + 100)
        first = self.atlas(max_nodes=1)
        self.assertIsNotNone(first["next_cursor"])
        clock.utc += 100
        self.fails(
            "atlas_resync_required", lambda: self.atlas(max_nodes=1, cursor=first["next_cursor"])
        )
        self.assertNotIn(claim["revision"], json.dumps(self.atlas()))

    def test_purge_removes_dependency_closure_and_denies_old_replay(self):
        self.accepted()
        before = self.atlas()
        self.store.purge(self.owner, self.obs["observation"])
        current = self.atlas()
        self.assertEqual(current["nodes"], [])
        self.assertEqual(current["edges"], [])
        with self.assertRaises(LocalError):
            self.run_r("replay", {"project": "p", "receipt": before["receipt"]})

    def test_large_unicode_page_enforces_byte_bound_without_orphans(self):
        for i in range(110):
            self.store.capture(
                self.owner,
                self.ns,
                f"atlas-large-{i}",
                {
                    "version": 1,
                    "project": "p",
                    "source": "atlas-test",
                    "source_event": str(i),
                    "text": "🔒" * 256,
                    "readers": None,
                    "classification": "internal",
                    "observed_at_ms": 1,
                },
            )
        result = self.atlas(max_nodes=500)
        from memoryking.contracts.canonical import canonical_bytes

        self.assertLessEqual(len(canonical_bytes(result)), 196608)
        self.assertIn("response_bytes", result["truncation"])
        self.assertIsNotNone(result["next_cursor"])
        self.assertEqual(len(result["nodes"]), sum(result["counts"].values()))
        ids = {n["id"] for n in result["nodes"]}
        self.assertTrue(all(e["source"] in ids and e["target"] in ids for e in result["edges"]))

    def test_filters_focus_edge_limits_and_invalid_bounds(self):
        claim = self.accepted()
        expanded = self.atlas(focus=[claim["revision"]])
        self.assertEqual(len(expanded["nodes"]), 2)
        self.assertEqual(len(expanded["edges"]), 1)
        claims = self.atlas(types=["claim"])
        self.assertEqual(len(claims["nodes"]), 1)
        self.assertEqual(claims["edges"], [])
        no_edges = self.atlas(max_edges=0)
        self.assertEqual(no_edges["edges"], [])
        self.assertIn("edge_limit", no_edges["truncation"])
        self.assertEqual(self.atlas(query="unrelated")["nodes"], [])
        for args in (
            {"max_nodes": 501},
            {"max_edges": 1501},
            {"focus": ["x"] * 17},
            {"types": ["unknown"]},
        ):
            self.fails("invalid_request", lambda args=args: self.atlas(**args))


class AtlasTests(AtlasCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown
    run_r = retrieval.RetrievalTests.run_r
    run_k = knowledge.KnowledgeTests.run_k
