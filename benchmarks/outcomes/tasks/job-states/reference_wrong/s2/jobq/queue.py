"""A small in-memory job queue with leases."""

import time
from dataclasses import dataclass

MAX_ATTEMPTS = 3


class LeaseError(Exception):
    """The job is not leased to that worker (any more)."""


@dataclass
class Job:
    id: int
    payload: object
    state: str = "pending"  # pending, running, done, failed
    attempts: int = 0
    available_at: float = 0.0
    lease_until: float | None = None
    worker: str | None = None
    error: str | None = None


class JobQueue:
    def __init__(self, clock=time.time):
        self.clock = clock
        self._jobs = {}
        self._next_id = 1

    def enqueue(self, payload, delay=0):
        """Add a job; it can be leased once `delay` seconds have passed."""
        job = Job(self._next_id, payload, available_at=self.clock() + delay)
        self._jobs[job.id] = job
        self._next_id += 1
        return job.id

    def get(self, job_id):
        return self._jobs[job_id]

    def lease(self, worker, ttl):
        """Give the lowest-id ready job to a worker, or None."""
        now = self.clock()
        for job_id in sorted(self._jobs):
            job = self._jobs[job_id]
            if job.state == "pending" and job.available_at <= now:
                job.state = "running"
                job.attempts += 1
                job.lease_until = now + ttl
                job.worker = worker
                return job
        return None

    def ack(self, job_id, worker):
        job = self._jobs[job_id]
        if job.state != "running" or job.worker != worker:
            raise LeaseError(job_id)
        job.state = "done"
        job.lease_until = None

    def _fail(self, job, reason):
        job.lease_until = None
        job.worker = None
        job.error = reason
        job.state = "failed" if job.attempts >= MAX_ATTEMPTS else "pending"

    def nack(self, job_id, worker):
        job = self._jobs[job_id]
        if job.state != "running" or job.worker != worker:
            raise LeaseError(job_id)
        self._fail(job, "nacked")

    def reap(self):
        """Fail running jobs whose lease has expired; returns how many."""
        now = self.clock()
        expired = [j for j in self._jobs.values() if j.state == "running" and j.lease_until <= now]
        for job in expired:
            self._fail(job, "expired")
        return len(expired)

    def counts(self):
        """Number of jobs per state."""
        result = {}
        for job in self._jobs.values():
            result[job.state] = result.get(job.state, 0) + 1
        return result
