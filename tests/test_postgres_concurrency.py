"""Race real independent PostgreSQL request stores, not one store's Python lock."""

import threading
from contextlib import contextmanager

import test_knowledge
import test_working
from test_postgres_parity import NativeFixture

from memoryking.adapters.postgres_store import PostgresStore


class PostgresIndependentRequests(NativeFixture):
    @contextmanager
    def actor(self, credential):
        with PostgresStore(
            self.pool,
            self.data,
            self.control,
            self.tenant,
            credential or self.owner,
            clock=self.store.clock,
        ) as store:
            if threading.current_thread() is not threading.main_thread():
                with self.pid_lock:
                    self.backend_pids.add(store.connection.info.backend_pid)
                self.barrier.wait(timeout=5)
            yield store

    def setUp(self):
        super().setUp()
        self.barrier = threading.Barrier(2)
        self.pid_lock = threading.Lock()
        self.backend_pids = set()

    def test_checkpoint_cas_across_independent_requests(self):
        test_working.WorkingTests.test_competing_cas_writers_have_one_winner(self)
        self.assertEqual(2, len(self.backend_pids))

    def test_review_cas_across_independent_requests(self):
        test_knowledge.KnowledgeTests.test_concurrent_review_optimistic_version(self)
        self.assertEqual(2, len(self.backend_pids))
