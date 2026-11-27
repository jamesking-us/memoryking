"""Durable projection mechanics on real stores; fixture vectors are not model-quality evidence."""

import json
import time
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

    def capture_more(self, *names):
        ids = {}
        for name in names:
            ids[name] = self.store.capture(
                self.owner,
                self.ns,
                name,
                {
                    "version": 1,
                    "project": "p",
                    "source": "fixture",
                    "source_event": name,
                    "text": f"observation {name}",
                    "readers": None,
                    "classification": "internal",
                    "observed_at_ms": 1,
                },
            )["observation"]
        return ids

    def claim_batch_e(self, configuration, count=16, credential=None):
        return self.run_e(
            "claim_batch",
            {"project": "p", "fingerprint": configuration["fingerprint"], "count": count},
            credential,
        )

    def complete_batch_e(self, jobs, credential=None, **changes):
        return self.run_e(
            "complete_batch",
            {
                "project": "p",
                "items": [
                    {
                        "lease": job["lease"],
                        "fingerprint": job["fingerprint"],
                        "input_hash": job["input_hash"],
                        "vector": [1000000] + [0] * 383,
                        "input_tokens": 12,
                        "truncated": False,
                        "duration_ms": 3,
                        **changes,
                    }
                    for job in jobs
                ],
            },
            credential,
        )

    def vector_subjects(self):
        with self.store.transaction():
            return {
                row[0]
                for row in self.store.db.execute(
                    "SELECT subject FROM embedding_vectors WHERE kind='observation'"
                ).fetchall()
            }

    def test_a_batch_claims_and_completes_each_job_as_a_single_call_would(self):
        ids = self.capture_more("b", "c")
        config = self.configure()
        first = self.claim_batch_e(config, 2)
        self.assertEqual(len(first["jobs"]), 2)
        rest = self.claim_batch_e(config, 16)
        self.assertEqual(len(rest["jobs"]), 1)  # the three observations, two then one
        jobs = first["jobs"] + rest["jobs"]
        self.assertEqual(len({job["lease"] for job in jobs}), 3)
        self.assertEqual(
            {job["observation"] for job in jobs} - set(ids.values()), {self.obs["observation"]}
        )
        self.assertEqual([job["job"] for job in jobs], sorted(job["job"] for job in jobs))
        done = self.complete_batch_e(jobs)
        self.assertEqual([item["status"] for item in done["results"]], ["complete"] * 3)
        self.assertEqual(self.vector_subjects(), {job["observation"] for job in jobs})
        idle = self.claim_batch_e(config)
        self.assertEqual((idle["jobs"], idle["job"], idle["ready"]), ([], None, True))
        self.assertTrue(self.status_e(config)["ready"])
        # Each lease works once: replaying the completion is refused and stores nothing new.
        again = self.complete_batch_e(jobs[:1])
        self.assertEqual(again["results"][0]["error"], "stale_lease")

    def test_a_record_purged_while_its_batch_is_in_flight_gets_no_vector(self):
        ids = self.capture_more("b", "c")
        config = self.configure()
        jobs = self.claim_batch_e(config)["jobs"]
        self.assertEqual(len(jobs), 3)
        self.store.purge(self.owner, ids["b"])
        done = self.complete_batch_e(jobs)
        # A purge anywhere in the tenant fences every lease in flight, so the others are
        # refused as well and claimed again; what matters is that no vector was stored.
        self.assertEqual({item["status"] for item in done["results"]}, {"refused"})
        self.assertEqual(self.vector_subjects(), set())

    def test_purge_removes_vectors_a_batch_stored(self):
        ids = self.capture_more("b", "c")
        jobs = self.claim_batch_e(self.configure())["jobs"]
        self.complete_batch_e(jobs)
        self.assertEqual(len(self.vector_subjects()), 3)
        self.store.purge(self.owner, ids["c"])
        self.assertEqual(self.vector_subjects(), {self.obs["observation"], ids["b"]})

    def partition_status(self):
        (item,) = [
            item
            for item in self.store.status(self.owner, "p")["semantic"]["partitions"]
            if item["state"] == "experimental"
        ]
        return item

    def test_status_counts_records_left_and_estimates_time_from_the_measured_rate(self):
        self.capture_more("b", "c")
        config = self.configure()
        first = self.partition_status()
        self.assertEqual(
            (first["records"], first["records_indexed"], first["records_left"]), (3, 0, 3)
        )
        self.assertNotIn("estimated_seconds", first)  # nothing measured yet, so no promise
        jobs = self.claim_batch_e(config)["jobs"]
        self.assertEqual(len(jobs), 3)
        self.store._embedding_mark = time.monotonic() - 2.0  # the last record finished 2 s ago
        self.complete_batch_e(jobs[:2])
        later = self.partition_status()
        self.assertEqual((later["records_indexed"], later["records_left"]), (2, 1))
        self.assertGreater(later["records_per_minute"], 0)
        self.assertIn(later["estimated_seconds"], {1, 2, 3})
        self.complete_batch_e(jobs[2:])
        self.assertTrue(self.status_e(config)["ready"])
        done = [
            item
            for item in self.store.status(self.owner, "p")["semantic"]["partitions"]
            if item["state"] == "experimental"
        ][0]
        self.assertNotIn("records_left", done)
        self.assertEqual(done["percent_indexed"], 100)

    def test_a_batch_obeys_reconfiguration_limits_and_permissions(self):
        self.capture_more("b")
        config = self.configure()
        jobs = self.claim_batch_e(config)["jobs"]
        reset = self.configure(generation=1)
        refused = self.complete_batch_e(jobs)["results"]
        self.assertEqual([item["error"] for item in refused], ["stale_lease"] * 2)
        self.assertEqual(self.vector_subjects(), set())
        self.fails("invalid_request", lambda: self.claim_batch_e(reset, 17))
        self.fails("invalid_request", lambda: self.claim_batch_e(reset, 0))
        worker = self.store.grant(self.owner, "worker", "p", ["jobs"])
        self.fails("denied", lambda: self.claim_batch_e(reset, 4, worker))
        worker = self.store.grant(self.owner, "worker", "p", ["jobs", "read"])
        held = self.claim_batch_e(reset, 16, worker)["jobs"]
        self.assertEqual(len(held), 2)
        self.store.grant(self.owner, "worker", "p", ["jobs"])
        self.fails("denied", lambda: self.complete_batch_e(held, worker))
        self.assertEqual(self.vector_subjects(), set())

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
                self.store.db.execute("SELECT count(*) FROM embedding_vectors").fetchone()[0], 2
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
                self.store.db.execute("SELECT count(*) FROM embedding_vectors").fetchone()[0], 0
            )


class LocalEmbeddingJobs(EmbeddingJobCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown
