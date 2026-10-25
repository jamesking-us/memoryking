"""Projection job parity on actual native PostgreSQL, with synthetic vector outputs."""

from test_embedding_jobs import EmbeddingJobCases
from test_postgres_parity import NativeFixture


class NativeEmbeddingJobs(EmbeddingJobCases, NativeFixture):
    pass
