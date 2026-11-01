"""Egress policy enforcement and audit records on native PostgreSQL."""

import http.server
import threading

import test_inference
from test_egress import EgressCases, FakeProvider
from test_postgres_inference import NativeClient
from test_postgres_parity import NativeFixture

from memoryking.adapters.postgres_store import PostgresStore


class PostgresEgressTests(EgressCases, NativeFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
        cls.provider_port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        super().tearDownClass()

    host = test_inference.InferenceTests.host
    fetch = test_inference.InferenceTests.fetch

    def setUp(self):
        super().setUp()
        self.ns = self.store.namespace(self.owner, "p")
        self.raw = "Observed timeout=30. A result, not a universal rule."
        self.obs = self.store.capture(
            self.owner,
            self.ns,
            "e",
            {
                "version": 1,
                "project": "p",
                "source": "test",
                "source_event": "e",
                "text": self.raw,
                "readers": None,
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )
        self.client = NativeClient(self)
        self.client.namespace = "sdk"

    def tearDown(self):
        PostgresStore.egress_policy = None
        super().tearDown()

    def set_policy(self, policy):
        # Every request opens its own store; the class default stands in for serve's flag.
        PostgresStore.egress_policy = policy
        self.store.egress_policy = policy
