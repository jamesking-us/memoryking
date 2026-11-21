import unittest

from jobq.queue import JobQueue
from tests.clock import FakeClock


class QueueTests(unittest.TestCase):
    def test_enqueue_and_get(self):
        q = JobQueue(FakeClock())
        first, second = q.enqueue({"n": 1}), q.enqueue({"n": 2})
        self.assertEqual((first, second), (1, 2))
        self.assertEqual(q.get(first).payload, {"n": 1})
        self.assertEqual(q.get(first).state, "pending")

    def test_delay_sets_availability(self):
        clock = FakeClock(100)
        q = JobQueue(clock)
        job_id = q.enqueue("x", delay=30)
        self.assertEqual(q.get(job_id).available_at, 130)

    def test_counts(self):
        q = JobQueue(FakeClock())
        q.enqueue("a")
        q.enqueue("b")
        self.assertEqual(q.counts(), {"pending": 2})


if __name__ == "__main__":
    unittest.main()
