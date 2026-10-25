"""Real optional extension, RLS and exact eligibility; no approximate index or model."""

import os
import unittest

import psycopg
from test_dense_retrieval import DenseCases
from test_postgres_parity import NativeFixture

from memoryking.adapters.postgres_admin import enable_vector
from memoryking.domain.local import LocalError


class PgvectorTests(DenseCases, NativeFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            with psycopg.connect(cls.cluster.dsn(), autocommit=True) as admin:
                available = admin.execute(
                    "SELECT 1 FROM pg_available_extension_versions WHERE name='vector' AND version='0.8.7'"
                ).fetchone()
                if not available:
                    if os.environ.get("MEMORYKING_REQUIRE_PGVECTOR") == "1":
                        raise RuntimeError("pgvector 0.8.7 is required for this job")
                    raise unittest.SkipTest("optional pgvector 0.8.7 is not installed")
                enable_vector(admin)
                enable_vector(admin)
        except BaseException:
            super().tearDownClass()
            raise

    def dense_args(self, config, **extra):
        result = super().dense_args(config, **extra)
        result["dense"]["engine"] = "pgvector_exact"
        return result

    def test_extension_engine_recorded_and_runtime_cannot_enable(self):
        config = self.configure()
        self.drain(config)
        result = self.run_r(args=self.dense_args(config))
        diag = self.receipt(result)["diagnostics"]["channel_frontiers"]["dense"]
        self.assertEqual(diag["method"], "pgvector_0_8_7_exact_cosine_v1")
        self.assertEqual(result["guidance"][0]["dense_score_ppm"], 1000000)
        with psycopg.connect(self.cluster.dsn("mk_runtime"), autocommit=True) as runtime:
            with self.assertRaisesRegex(LocalError, "extension_administrator_required"):
                enable_vector(runtime)
        with psycopg.connect(self.cluster.dsn(), autocommit=True) as admin:
            indexes = admin.execute(
                "SELECT count(*) FROM pg_indexes WHERE schemaname='memoryking' AND (indexdef LIKE '%hnsw%' OR indexdef LIKE '%ivfflat%')"
            ).fetchone()[0]
            self.assertEqual(indexes, 0)
