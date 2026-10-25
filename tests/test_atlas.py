"""Atlas assertions shared by actual SQLite and native PostgreSQL services."""

import json
import unittest

import test_knowledge as knowledge
import test_retrieval as retrieval

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
            self.fails("invalid_request", lambda: self.atlas(**args))


class AtlasTests(AtlasCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown
    run_r = retrieval.RetrievalTests.run_r
    run_k = knowledge.KnowledgeTests.run_k
