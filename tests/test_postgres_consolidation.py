"""Consolidation on native PostgreSQL: the same admission rules through the shared store."""

from test_consolidation import ConsolidationCases
from test_postgres_inference import NativeClient
from test_postgres_parity import NativeFixture

from memoryking.adapters.postgres_store import PostgresStore


class PostgresConsolidationTests(ConsolidationCases, NativeFixture):
    def setUp(self):
        super().setUp()
        self.client = NativeClient(self)
        self.client.namespace = "sdk"
        self.policy = None

    def tearDown(self):
        PostgresStore.egress_policy = None
        super().tearDown()

    def set_policy(self, policy):
        # Every request opens its own store; the class default stands in for serve's flag.
        PostgresStore.egress_policy = policy
        self.store.egress_policy = policy
