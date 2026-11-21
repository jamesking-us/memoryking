import unittest

from jobq.queue import JobQueue, LeaseError
from tests.clock import FakeClock


class LeaseTests(unittest.TestCase):
    def test_lease_and_ack(self):
        clock = FakeClock(100)
        q = JobQueue(clock)
        q.enqueue("a")
        q.enqueue("b", delay=50)
        job = q.lease("w1", 10)
        self.assertEqual((job.payload, job.state, job.attempts, job.lease_until), ("a", "running", 1, 110))
        self.assertIsNone(q.lease("w2", 10))
        clock.advance(60)
        self.assertEqual(q.lease("w2", 10).payload, "b")
        q.ack(job.id, "w1")
        self.assertEqual(q.get(job.id).state, "done")

    def test_ack_checks_worker(self):
        q = JobQueue(FakeClock())
        q.enqueue("a")
        job = q.lease("w1", 10)
        with self.assertRaises(LeaseError):
            q.ack(job.id, "w2")
        q.ack(job.id, "w1")
        with self.assertRaises(LeaseError):
            q.ack(job.id, "w1")


if __name__ == "__main__":
    unittest.main()
