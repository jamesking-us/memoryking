import unittest

from jobq import stats
from jobq.queue import JobQueue
from tests.clock import FakeClock


class StatsTests(unittest.TestCase):
    def test_depth_and_wait(self):
        clock = FakeClock(100)
        q = JobQueue(clock)
        q.enqueue("a")
        q.enqueue("b", delay=50)
        clock.advance(10)
        self.assertEqual(stats.depth(q), 1)
        self.assertEqual(stats.oldest_wait(q), 10)
        clock.advance(100)
        self.assertEqual(stats.depth(q), 2)

    def test_empty(self):
        q = JobQueue(FakeClock())
        self.assertEqual((stats.depth(q), stats.oldest_wait(q), stats.by_worker(q)), (0, 0, {}))


if __name__ == "__main__":
    unittest.main()
