"""Durable projection mechanics on real stores; fixture vectors are not model-quality evidence."""

import json
import unittest

import test_knowledge as knowledge
import test_retrieval as retrieval
from recovery_helpers import backup_fixture, restore_fixture

from memoryking.adapters.embedding_store import Embeddings
from memoryking.contracts.canonical import canonical_hash


class EmbeddingJobCases:
    fails = retrieval.RetrievalTests.fails
    clock = retrieval.RetrievalTests.clock

    def run_e(self, action, args, credential=None, key=None):
        self.n += 1
        command = {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": action,
            "args": args,
        }
        if action == "configure":
            command["key"] = key or f"e{self.n}"
        if hasattr(self, "service"):
            return self.service(Embeddings, command, credential)
        output = []
        Embeddings(self.store).execute(credential or self.owner, command).emit(output.append)
        return json.loads(output[0])

    def configure(self, generation=0, state="experimental", key=None):
        return self.run_e(
            "configure",
            {
                "project": "p",
                "model": "minilm-l6-v2-onnx",
                "state": state,
                "expected_generation": generation,
            },
            key=key,
        )

    def claim_e(self, configuration, credential=None):
        return self.run_e(
            "claim", {"project": "p", "fingerprint": configuration["fingerprint"]}, credential
        )

    def complete_e(self, lease, **changes):
        return self.run_e(
            "complete",
            {
                "project": "p",
                "lease": lease["lease"],
                "fingerprint": lease["fingerprint"],
                "input_hash": lease["input_hash"],
                "vector": [1000000] + [0] * 383,
                "input_tokens": 12,
                "truncated": False,
                "duration_ms": 3,
                **changes,
            },
        )

    def status_e(self, configuration):
        return self.run_e("status", {"project": "p", "fingerprint": configuration["fingerprint"]})

    def test_partition_configuration_retries_and_durable_reordered_frontier(self):
        self.store.capture(
            self.owner,
            self.ns,
            "second",
            {
                "version": 1,
                "project": "p",
                "source": "fixture",
                "source_event": "second",
                "text": "another observation",
                "readers": None,
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )
        config = self.configure(key="same")
        self.assertTrue(self.configure(key="same")["replayed"])
        self.assertFalse(self.status_e(config)["ready"])
        first, second = self.claim_e(config), self.claim_e(config)
        self.assertLess(first["job"], second["job"])
        self.complete_e(second)
        self.assertEqual(self.status_e(config)["frontier"], 0)
        self.complete_e(first)
        self.assertTrue(self.status_e(config)["ready"])
        with self.store.transaction():
            self.assertEqual(
                self.store.db.execute("SELECT count(*) FROM embedding_items").fetchone()[0], 2
            )
        self.fails("stale_lease", lambda: self.complete_e(first))
        self.fails("embedding_generation_conflict", lambda: self.configure())

    def test_wrong_fingerprint_input_hash_dimension_and_zero_vectors_reject(self):
        config = self.configure()
        lease = self.claim_e(config)
        for code, changes in [
            ("embedding_fingerprint_mismatch", {"fingerprint": "0" * 64}),
            ("embedding_batch_invalid", {"input_hash": "0" * 64}),
            ("embedding_vector_invalid", {"vector": [1000000, 0]}),
            ("embedding_vector_invalid", {"vector": [0] * 384}),
        ]:
            self.fails(code, lambda changes=changes: self.complete_e(lease, **changes))
        self.complete_e(lease)
        self.assertTrue(self.status_e(config)["ready"])

    def test_outage_leaves_unknown_usage_and_backoff_then_reclaims(self):
        clock = self.clock()
        config = self.configure()
        lease = self.claim_e(config)
        result = self.run_e(
            "fail",
            {
                "project": "p",
                "lease": lease["lease"],
                "reason": "provider_unavailable",
                "input_tokens": None,
                "duration_ms": 10,
            },
        )
        self.assertEqual(result["status"], "pending")
        self.assertIsNone(self.claim_e(config)["job"])
        clock.utc += 2000
        clock.mono += 2000
        retried = self.claim_e(config)
        self.assertEqual(retried["job"], lease["job"])
        self.fails("stale_lease", lambda: self.complete_e(lease))
        self.complete_e(retried)
        with self.store.transaction():
            usage = self.store.db.execute(
                "SELECT status,input_tokens FROM processing_usage ORDER BY created"
            ).fetchall()
            self.assertEqual([(r[0], r[1]) for r in usage], [("failed", None), ("succeeded", 12)])

    def test_reconfigure_purge_and_restore_fence_old_derived_work(self):
        config = self.configure()
        lease = self.claim_e(config)
        reset = self.configure(generation=1)
        self.fails("stale_lease", lambda: self.complete_e(lease))
        lease = self.claim_e(reset)
        archive = backup_fixture(self)
        self.store.purge(self.owner, self.obs["observation"])
        self.fails("stale_lease", lambda: self.complete_e(lease))
        restore_fixture(self, archive)
        self.fails("history_changed", lambda: self.complete_e(lease))
        self.assertEqual(self.status_e(config)["state"], "disabled")
        self.fails("embedding_disabled", lambda: self.claim_e(config))

    def test_source_read_permission_is_required_before_input_leaves_service(self):
        config = self.configure()
        worker = self.store.grant(self.owner, "worker", "p", ["jobs"])
        self.fails("denied", lambda: self.claim_e(config, worker))
        worker = self.store.grant(self.owner, "worker", "p", ["jobs", "read"])
        # Explicit private evidence is never released even to a projection worker.
        private = self.store.capture(
            self.owner,
            self.ns,
            "private",
            {
                "version": 1,
                "project": "p",
                "source": "fixture",
                "source_event": "private",
                "text": "private hidden content",
                "readers": ["owner"],
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )
        public = self.claim_e(config, worker)
        self.assertNotEqual(public["observation"], private["observation"])
        no_work = self.claim_e(config, worker)
        self.assertNotIn("input", no_work)
        self.assertIsNone(no_work["job"])
        self.assertNotIn(private["observation"], json.dumps(no_work))
        self.assertEqual(public["input_hash"], canonical_hash(public["input"]))

    def test_revoked_worker_read_cannot_commit_previously_leased_output(self):
        config = self.configure()
        worker = self.store.grant(self.owner, "worker", "p", ["jobs", "read"])
        lease = self.claim_e(config, worker)
        self.store.grant(self.owner, "worker", "p", ["jobs"])
        self.fails(
            "denied",
            lambda: self.run_e(
                "complete",
                {
                    "project": "p",
                    "lease": lease["lease"],
                    "fingerprint": lease["fingerprint"],
                    "input_hash": lease["input_hash"],
                    "vector": [1000000] + [0] * 383,
                    "input_tokens": 12,
                    "truncated": False,
                    "duration_ms": 3,
                },
                worker,
            ),
        )
        with self.store.transaction():
            self.assertEqual(
                self.store.db.execute("SELECT count(*) FROM embedding_items").fetchone()[0], 0
            )


class LocalEmbeddingJobs(EmbeddingJobCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown
