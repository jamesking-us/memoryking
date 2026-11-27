"""Real optional extension: RLS, exact eligibility, the HNSW index and its filtered shortfall."""

import json
import math
import os
import unittest

import psycopg
from test_dense_retrieval import DenseCases
from test_postgres_parity import NativeFixture

from memoryking.adapters import dense_search, postgres_admin
from memoryking.adapters.postgres_admin import enable_vector
from memoryking.contracts.canonical import canonical_hash
from memoryking.domain.embeddings import pack
from memoryking.domain.local import LocalError


class PgvectorTests(DenseCases, NativeFixture):
    version = "0.8.7"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            with psycopg.connect(cls.cluster.dsn(), autocommit=True) as admin:
                found = admin.execute(
                    "SELECT version FROM pg_available_extension_versions WHERE name='vector' ORDER BY version DESC"
                ).fetchall()
                versions = [row[0] for row in found]
                if not versions:
                    if os.environ.get("MEMORYKING_REQUIRE_PGVECTOR") == "1":
                        raise RuntimeError("pgvector is required for this job")
                    raise unittest.SkipTest("optional pgvector is not installed")
                if "0.8.7" not in versions:
                    # The release this build pins is not installable here. Any release with
                    # HNSW (0.5 and later) exercises the same statements; one without
                    # iterative scans also exercises the filtered shortfall for real.
                    if os.environ.get("MEMORYKING_REQUIRE_PGVECTOR") == "1":
                        raise RuntimeError("pgvector 0.8.7 is required for this job")
                    cls.version = next((v for v in versions if v >= "0.5"), "")
                    if not cls.version:
                        raise unittest.SkipTest("pgvector with HNSW (0.5 or later) is needed")
                    cls.pinned = postgres_admin.PGVECTOR_VERSIONS
                    postgres_admin.PGVECTOR_VERSIONS = (cls.version,)
                enable_vector(admin)
                enable_vector(admin)  # safe to run again
        except BaseException:
            cls.restore_pin()
            super().tearDownClass()
            raise

    @classmethod
    def restore_pin(cls):
        if "pinned" in cls.__dict__:
            postgres_admin.PGVECTOR_VERSIONS = cls.pinned

    @classmethod
    def tearDownClass(cls):
        cls.restore_pin()
        super().tearDownClass()

    def dense_args(self, config, **extra):
        result = super().dense_args(config, **extra)
        result["dense"]["engine"] = "pgvector_exact"
        return result

    def test_extension_engine_and_the_index_are_present_and_runtime_cannot_enable(self):
        config = self.configure()
        self.drain(config)
        result = self.run_r(args=self.dense_args(config))
        self.assertEqual(result["guidance"][0]["dense_score_ppm"], 1000000)
        with psycopg.connect(self.cluster.dsn("mk_runtime"), autocommit=True) as runtime:
            with self.assertRaisesRegex(LocalError, "extension_administrator_required"):
                enable_vector(runtime)
        with psycopg.connect(self.cluster.dsn(), autocommit=True) as admin:
            indexes = admin.execute(
                "SELECT count(*) FROM pg_indexes WHERE schemaname='memoryking' AND indexdef LIKE '%hnsw%'"
            ).fetchone()[0]
            self.assertEqual(indexes, 1)
            # The converted copy follows its source row, including its removal.
            native = admin.execute(
                "SELECT count(*) FROM memoryking.embedding_native WHERE tenant=%s", (self.tenant,)
            ).fetchone()[0]
            self.assertEqual(native, 1)
        self.store.purge(self.owner, self.obs["observation"])
        with psycopg.connect(self.cluster.dsn(), autocommit=True) as admin:
            self.assertEqual(
                admin.execute(
                    "SELECT count(*) FROM memoryking.embedding_native WHERE tenant=%s",
                    (self.tenant,),
                ).fetchone()[0],
                0,
            )

    def fill(self, count, hidden_to):
        """`count` synthetic observations whose vectors lean toward the query direction more
        the lower their number; the first `hidden_to` are readable by the owner only."""
        config = self.configure()
        with self.store.transaction():
            original = dict(
                self.store.db.execute(
                    "SELECT * FROM observations WHERE tenant=? AND id=?",
                    (self.tenant, self.obs["observation"]),
                ).fetchone()
            )
            position = self.store.db.execute(
                "SELECT position FROM tenants WHERE tenant=?", (self.tenant,)
            ).fetchone()[0]
            for index in range(count):
                row = dict(original, id=f"obs_fill_{index:04}", source_event=f"fill_{index}")
                if index < hidden_to:
                    row["readers"] = json.dumps(["owner"])
                self.store.db.execute(
                    "INSERT INTO observations VALUES (" + ",".join("?" for _ in row) + ")",
                    tuple(row.values()),
                )
                angle = 0.001 + index * 0.0012
                vector = [round(1e6 * math.cos(angle)), round(1e6 * math.sin(angle))] + [0] * 382
                self.store.db.execute(
                    "INSERT INTO embedding_vectors VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        self.tenant,
                        "p",
                        config["fingerprint"],
                        config["generation"],
                        "observation",
                        row["id"],
                        0,
                        self.store.incarnation,
                        row["revision"],
                        row["blob"],
                        "0" * 64,
                        pack(vector),
                        0,
                        10,
                        None,
                        0,
                        position,
                    ),
                )
            self.store.db.execute(
                "UPDATE embedding_partitions SET scheduled=?,frontier=? WHERE tenant=?",
                (position, position, self.tenant),
            )
        return config

    def search(self, config, reader):
        who = {"tenant": self.tenant, "principal": reader}
        args = {
            "project": "p",
            "query": "q",
            "dense": {
                "fingerprint": config["fingerprint"],
                "query_hash": canonical_hash("q"),
                "vector": [1000000] + [0] * 383,
                "minimum_similarity_ppm": 0,
            },
        }
        with self.store.transaction():
            return dense_search.search(self.store, who, args, 10**9, 10**9)

    def test_a_large_visible_corpus_is_answered_from_the_index_and_rescored_exactly(self):
        if dense_search.numpy_module() is None:
            self.skipTest("the exact rescore needs numpy, which the embeddings extra installs")
        config = self.fill(400, 0)
        found = self.search(config, "owner")
        self.assertEqual(found["scan"], "hnsw_shortlist_exact_rescore_v1")
        self.assertEqual(
            found["method"], f"pgvector_{self.version.replace('.', '_')}_hnsw_exact_rescore_v1"
        )
        exact = dense_search.numpy_module()
        self.assertIsNotNone(exact)
        # The exact scan (forced by asking for the python engine on the same store) agrees on
        # the best of the top scores.
        with self.store.transaction():
            who = {"tenant": self.tenant, "principal": "owner"}
            args = {
                "project": "p",
                "query": "q",
                "dense": {
                    "fingerprint": config["fingerprint"],
                    "query_hash": canonical_hash("q"),
                    "vector": [1000000] + [0] * 383,
                    "minimum_similarity_ppm": 0,
                },
            }
            self.store.native_index = None
            try:
                baseline = dense_search.search(self.store, who, args, 10**9, 10**9)
            finally:
                del self.store.native_index
        self.assertEqual(baseline["scan"], "numpy_matrix_v1")
        top = lambda scores: sorted(scores, key=lambda s: (-scores[s], s))[:20]  # noqa: E731
        self.assertEqual(top(found["scores"]), top(baseline["scores"]))

    def test_a_reader_who_sees_little_of_a_large_partition_still_gets_every_visible_match(self):
        # Eleven hundred hidden vectors lie nearest the query, more than one shortlist holds;
        # the reader can see only the last hundred. A filtered approximate search would crowd them out and under-return;
        # the answer must fall back to the exact, eligibility-first scan.
        config = self.fill(1200, 1100)
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        del reader
        found = self.search(config, "reader")
        self.assertNotEqual(found["scan"], "hnsw_shortlist_exact_rescore_v1")
        self.assertEqual(found["eligible_vectors"], 100)
        # Every candidate the stage may return is one the reader can see: the hidden vectors
        # take none of its places.
        self.assertEqual(len(found["scores"]), min(100, dense_search.RESULTS))
        self.assertEqual(found["candidate_limit_reached"], dense_search.RESULTS < 100)
        self.assertFalse(any(key < "obs_fill_1100" for key in found["scores"]))
