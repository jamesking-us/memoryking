"""Real eligibility, fusion and release gates; synthetic vectors are fixture geometry."""

import unittest
from unittest.mock import patch

import test_embedding_jobs as jobs
import test_knowledge as knowledge
import test_retrieval as retrieval

from memoryking.contracts.canonical import canonical_hash


class DenseCases:
    run_e = jobs.EmbeddingJobCases.run_e
    configure = jobs.EmbeddingJobCases.configure
    claim_e = jobs.EmbeddingJobCases.claim_e
    complete_e = jobs.EmbeddingJobCases.complete_e
    fails = retrieval.RetrievalTests.fails
    brief_args = retrieval.RetrievalTests.brief_args
    receipt = retrieval.RetrievalTests.receipt
    body = retrieval.RetrievalTests.body
    propose = retrieval.RetrievalTests.propose
    review = retrieval.RetrievalTests.review
    constraint = retrieval.RetrievalTests.constraint

    def dense_args(self, config, **extra):
        return self.brief_args(
            query="latency",
            dense={
                "fingerprint": config["fingerprint"],
                "query_hash": canonical_hash("latency"),
                "vector": [1000000] + [0] * 383,
            },
            **extra,
        )

    def drain(self, config):
        for _ in range(32):
            job = self.claim_e(config)
            if job["job"] is None:
                return
            self.complete_e(job)
        self.fail("fixture did not drain")

    def test_incomplete_then_ready_without_stale_cached_absence(self):
        config = self.configure()
        required = {"required_channels": ["lexical", "dense"]}
        self.fails(
            "index_not_ready",
            lambda: self.run_r(args=self.dense_args(config, consistency=required)),
        )
        pending = self.run_r(args=self.dense_args(config))
        self.assertEqual(pending["coverage"]["dense"], "indexing")
        self.assertEqual(pending["guidance"], [])
        self.assertEqual(pending["status"], "partial")
        self.drain(config)
        result = self.run_r(args=self.dense_args(config, consistency=required))
        self.assertEqual(result["coverage"]["dense"], "ready")
        self.assertEqual(result["guidance"][0]["reference"], self.obs["observation"])
        self.assertEqual(result["guidance"][0]["selection_reason"], "semantic_similarity")
        self.assertEqual(result["guidance"][0]["authority"], "observed_episode")
        diag = self.receipt(result)["diagnostics"]
        self.assertEqual(diag["model_fingerprints"], [config["fingerprint"]])
        self.assertEqual(diag["trace"][-2]["metric"], "equal_channel_rrf60_v1")
        replay = self.run_r("replay", {"project": "p", "receipt": result["receipt"]})
        self.assertEqual(result, replay)

    def test_dense_never_competes_with_mandatory_or_bypasses_budget(self):
        self.constraint()
        config = self.configure()
        self.drain(config)
        result = self.run_r(args=self.dense_args(config, top_k=0))
        self.assertEqual(len(result["constraints"]), 1)
        self.assertEqual(result["guidance"], [])
        result = self.run_r(args=self.dense_args(config, max_output_tokens=100))
        self.assertEqual(result["status"], "budget_insufficient")
        attested = self.run_r(args=self.dense_args(config, freshness="attested"))
        self.assertEqual(attested["coverage"]["dense"], "unsupported_freshness")

    def test_filter_precedes_scan_limit_and_purge_invalidates_replay(self):
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        for i in range(3):
            self.store.capture(
                self.owner,
                self.ns,
                f"hidden{i}",
                {
                    "version": 1,
                    "project": "p",
                    "source": "fixture",
                    "source_event": f"hidden{i}",
                    "text": "secret latency",
                    "readers": ["owner"],
                    "classification": "internal",
                    "observed_at_ms": 1,
                },
            )
        config = self.configure()
        self.drain(config)
        with patch("memoryking.adapters.dense_search.SCAN_LIMIT", 1):
            result = self.run_r(args=self.dense_args(config), credential=reader)
            self.assertEqual(result["coverage"]["dense"], "ready")
            self.assertEqual(
                [i["reference"] for i in result["guidance"]], [self.obs["observation"]]
            )
            owner = self.run_r(args=self.dense_args(config))
            self.assertEqual(owner["coverage"]["dense"], "scan_limit")
        self.store.purge(self.owner, self.obs["observation"])
        self.fails(
            "replay_purged",
            lambda: self.run_r("replay", {"project": "p", "receipt": result["receipt"]}, reader),
        )
        after = self.run_r(args=self.dense_args(config), credential=reader)
        self.assertEqual(after["guidance"], [])

    def test_daemon_model_fills_dense_queries_only_for_enabled_projects(self):
        from memoryking.contracts.canonical import canonical_hash

        class FakeEmbedder:
            def __init__(self, fingerprint):
                self.fingerprint, self.dimensions, self.calls = fingerprint, 384, []

            def embed(self, texts):
                self.calls.extend(texts)
                vectors = [[1000000] + [0] * 383 for _ in texts]
                return type("Batch", (), {"vectors": vectors})()

        before = self.run_r()
        self.assertEqual(before["coverage"]["dense"], "unavailable")
        config = self.configure()
        job = self.claim_e(config)
        self.complete_e(job)
        self.store.embedder = FakeEmbedder(config["fingerprint"])
        result = self.run_r()
        self.assertEqual(result["coverage"]["dense"], "ready")
        self.assertEqual(self.store.embedder.calls, [self.brief_args()["query"]])
        diag = self.receipt(result)["diagnostics"]
        self.assertEqual(diag["model_fingerprints"], [config["fingerprint"]])
        # An explicit dense request is never overridden by the daemon's model.
        explicit = self.dense_args(config)
        explicit["dense"]["query_hash"] = canonical_hash(explicit["query"])
        self.run_r(args=explicit)
        self.assertEqual(len(self.store.embedder.calls), 1)
        # A different model fingerprint means no matching partition: lexical only.
        self.store.embedder = FakeEmbedder("0" * 64)
        self.assertEqual(self.run_r()["coverage"]["dense"], "unavailable")

    def test_fingerprint_query_dimension_and_zero_vector_validation(self):
        config = self.configure()
        self.drain(config)
        args = self.dense_args(config)
        args["dense"]["query_hash"] = "0" * 64
        self.fails("embedding_query_mismatch", lambda: self.run_r(args=args))
        for vector in ([1], [0] * 384):
            args = self.dense_args(config)
            args["dense"]["vector"] = vector
            self.fails("embedding_vector_invalid", lambda args=args: self.run_r(args=args))
        args = self.dense_args(config)
        args["dense"]["fingerprint"] = "0" * 64
        result = self.run_r(args=args)
        self.assertEqual(result["coverage"]["dense"], "unavailable")
        self.assertEqual(result["status"], "partial")

    def test_native_engine_availability_is_explicit_and_lexical_still_works(self):
        config = self.configure()
        self.drain(config)
        args = self.dense_args(config)
        args["dense"]["engine"] = "pgvector_exact"
        result = self.run_r(args=args)
        self.assertEqual(
            result["coverage"]["dense"],
            "ready" if type(self).__name__ == "PgvectorTests" else "engine_unavailable",
        )
        lexical = self.run_r()
        self.assertEqual(lexical["guidance"][0]["reference"], self.obs["observation"])


class DenseRetrievalTests(DenseCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown
    run_k = knowledge.KnowledgeTests.run_k
    run_r = retrieval.RetrievalTests.run_r
