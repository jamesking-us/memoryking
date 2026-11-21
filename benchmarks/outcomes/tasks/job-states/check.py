#!/usr/bin/env python3
"""Hidden check for job-states: run as `check.py WORKDIR`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import checklib  # noqa: E402

HIDDEN = '''
import unittest

from jobq.queue import JobQueue, LeaseError


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class Hidden(unittest.TestCase):
    def test_lease_and_ack(self):
        clock = Clock()
        q = JobQueue(clock)
        a, b = q.enqueue("a"), q.enqueue("b", delay=5)
        job = q.lease("w1", 20)
        self.assertEqual((job.id, job.state, job.attempts, job.lease_until, job.worker), (a, "running", 1, 1020, "w1"))
        self.assertIsNone(q.lease("w2", 20))
        with self.assertRaises(LeaseError):
            q.ack(a, "w2")
        q.ack(a, "w1")
        self.assertEqual(q.get(a).state, "done")
        clock.now += 5
        self.assertEqual(q.lease("w2", 20).id, b)

    def test_backoff_schedule_and_dead_state(self):
        clock = Clock(0)
        q = JobQueue(clock)
        job_id = q.enqueue("x")
        waits = []
        for attempt in range(1, 5):
            job = q.lease("w", 10)
            self.assertEqual(job.attempts, attempt)
            q.nack(job_id, "w")
            self.assertEqual(q.get(job_id).state, "pending")
            waits.append(q.get(job_id).available_at - clock.now)
            self.assertIsNone(q.lease("w", 10), "still waiting out the backoff")
            clock.now += waits[-1] - 1
            self.assertIsNone(q.lease("w", 10))
            clock.now += 1
        self.assertEqual(waits, [30, 60, 120, 240])
        q.lease("w", 10)
        self.assertEqual(q.get(job_id).attempts, 5)
        q.nack(job_id, "w")
        job = q.get(job_id)
        self.assertEqual((job.state, job.dead_reason), ("dead", "nacked"))
        clock.now += 10**6
        self.assertIsNone(q.lease("w", 10), "dead jobs are never retried")
        self.assertEqual(q.counts(), {"dead": 1})

    def test_expired_leases_are_reaped_as_expired(self):
        clock = Clock(0)
        q = JobQueue(clock)
        q.enqueue("x")
        q.enqueue("y")
        q.lease("w", 10)
        q.lease("w", 100)
        clock.now = 9
        self.assertEqual(q.reap(), 0)
        clock.now = 10
        self.assertEqual(q.reap(), 1)
        self.assertEqual((q.get(1).state, q.get(1).available_at), ("pending", 40))
        self.assertEqual(q.get(2).state, "running")
        with self.assertRaises(LeaseError):
            q.nack(1, "w")
        for _ in range(4):
            clock.now = q.get(1).available_at
            q.lease("w", 1)
            clock.now += 1
            q.reap()
        job = q.get(1)
        self.assertEqual((job.state, job.dead_reason, job.attempts), ("dead", "expired", 5))

    def test_nack_requires_the_lease(self):
        q = JobQueue(Clock())
        q.enqueue("x")
        with self.assertRaises(LeaseError):
            q.nack(1, "w")
        q.lease("w", 10)
        with self.assertRaises(LeaseError):
            q.nack(1, "other")

unittest.main()
'''


def main() -> int:
    workdir = checklib.workdir_arg()
    report = checklib.Report()
    ok, detail = checklib.hidden_tests(workdir, HIDDEN)
    report.add("hidden tests", ok, detail)
    return report.finish()


if __name__ == "__main__":
    raise SystemExit(main())
