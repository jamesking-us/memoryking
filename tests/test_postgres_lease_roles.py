"""The instance lease under each PostgreSQL role, across its renewal boundary.

Only the runtime role may write `instance_lease`; the worker and backup roles can read it.
A request under any other role that happens to be the first after the 15-second renewal
interval must compare and leave renewal to the next runtime writer.
"""

import unittest

import psycopg
import test_postgres_parity as parity
from test_local_store import FakeClock

from memoryking.adapters.postgres_pool import create_pool
from memoryking.adapters.postgres_store import PostgresStore
from memoryking.application.store_core import LEASE_EXPIRY_MS, LEASE_RENEW_MS
from memoryking.domain.local import LocalError


class LeaseRoleTests(parity.NativeFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.worker_pool = create_pool(cls.cluster.dsn("mk_worker"), size=2)

    @classmethod
    def tearDownClass(cls):
        cls.worker_pool.close()
        super().tearDownClass()

    def build(self, pool=None, **kwargs):
        return PostgresStore(
            pool or self.pool, self.data, self.control, self.tenant, self.owner, **kwargs
        )

    def touch(self, store):
        with store.transaction():
            pass

    def admin(self, statement):
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as connection:
            with connection.transaction():
                connection.execute("SET LOCAL search_path=memoryking,pg_catalog")
                connection.execute("SELECT set_config('memoryking.tenant',%s,true)", (self.tenant,))
                result = connection.execute(statement)
                return result.fetchone() if result.description else None

    def row(self):
        found = self.admin("SELECT holder,renewed_ms FROM instance_lease")
        return None if found is None else (found[0], found[1])

    def refused(self, store):
        with self.assertRaises(LocalError) as caught:
            self.touch(store)
        self.assertEqual(caught.exception.code, "control_lease_held")

    def kinds(self, clock):
        """Each request kind under the role it really runs as."""
        return {
            "runtime write": lambda: self.build(clock=clock),
            "runtime read": lambda: self.build(clock=clock, release_only=True),
            "worker": lambda: self.build(self.worker_pool, clock=clock),
            "worker read": lambda: self.build(self.worker_pool, clock=clock, release_only=True),
        }

    def test_every_request_kind_passes_across_the_renewal_boundary(self):
        clock = FakeClock()
        with self.build(clock=clock) as store:
            self.touch(store)
        holder, renewed = self.row()
        for name, make in self.kinds(clock).items():
            with self.subTest(kind=name):
                clock.advance(LEASE_RENEW_MS)  # the first request after the interval
                with make() as store:
                    self.touch(store)
        self.assertEqual(self.row()[0], holder)
        self.assertGreater(self.row()[1], renewed)

    def test_a_worker_never_claims_or_renews_the_lease(self):
        clock = FakeClock()
        self.admin("DELETE FROM instance_lease")
        with self.build(self.worker_pool, clock=clock) as worker:
            self.touch(worker)
        self.assertIsNone(self.row())  # the claim is left to the next runtime writer
        with self.build(clock=clock) as store:
            self.touch(store)
        holder, renewed = self.row()
        clock.advance(LEASE_RENEW_MS + 1)
        with self.build(self.worker_pool, clock=clock) as worker:
            self.touch(worker)
        self.assertEqual(self.row(), (holder, renewed))

    def test_a_worker_of_another_holder_is_still_refused(self):
        clock = FakeClock()
        with self.build(clock=clock) as store:
            self.touch(store)
        with self.build(self.worker_pool, clock=clock, instance_id="alpha") as worker:
            self.refused(worker)

    def test_a_read_only_workload_past_the_expiry_blocks_neither_the_holder_nor_a_takeover(self):
        clock = FakeClock()
        with self.build(clock=clock) as store:
            self.touch(store)
        holder, renewed = self.row()
        for _ in range(8):  # 80 seconds of readers and workers only
            clock.advance(10_000)
            for name, make in self.kinds(clock).items():
                if name != "runtime write":
                    with make() as store:
                        self.touch(store)
        self.assertGreater(clock.utc_ms() - renewed, LEASE_EXPIRY_MS)
        self.assertEqual(self.row(), (holder, renewed))  # readers never wrote it
        # The legitimate holder's next write renews it and is not refused.
        with self.build(clock=clock) as store:
            self.touch(store)
        self.assertEqual(self.row(), (holder, clock.utc_ms()))
        # Another holder is refused while that renewal is fresh, whoever else is reading.
        with self.build(clock=clock, instance_id="alpha") as other:
            self.refused(other)
        # Un-renewed for the whole expiry, another holder's writer takes over; the first holder
        # is then refused rather than both being admitted.
        clock.advance(LEASE_EXPIRY_MS)
        with self.build(clock=clock, instance_id="alpha") as other:
            self.touch(other)
        self.assertNotEqual(self.row()[0], holder)
        with self.build(clock=clock) as store:
            self.refused(store)


if __name__ == "__main__":
    unittest.main()
