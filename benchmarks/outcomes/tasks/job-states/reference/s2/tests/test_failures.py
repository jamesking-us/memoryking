import unittest

from jobq.queue import JobQueue, LeaseError
from tests.clock import FakeClock


class FailureTests(unittest.TestCase):
    def test_backoff_then_dead(self):
        clock = FakeClock(0)
        q = JobQueue(clock)
        job_id = q.enqueue("a")
        waits = []
        for _ in range(4):
            job = q.lease("w", 10)
            q.nack(job.id, "w")
            waits.append(q.get(job_id).available_at - clock.now)
            self.assertIsNone(q.lease("w", 10))
            clock.advance(waits[-1])
        self.assertEqual(waits, [30, 60, 120, 240])
        q.lease("w", 10)
        q.nack(job_id, "w")
        self.assertEqual((q.get(job_id).state, q.get(job_id).dead_reason), ("dead", "nacked"))

    def test_reap_expired(self):
        clock = FakeClock(0)
        q = JobQueue(clock)
        q.enqueue("a")
        q.lease("w", 10)
        self.assertEqual(q.reap(), 0)
        clock.advance(10)
        self.assertEqual(q.reap(), 1)
        self.assertEqual(q.get(1).state, "pending")
        with self.assertRaises(LeaseError):
            q.nack(1, "w")


if __name__ == "__main__":
    unittest.main()
