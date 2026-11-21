"""A small in-memory job queue with leases."""

import time
from dataclasses import dataclass


class LeaseError(Exception):
    """The job is not leased to that worker (any more)."""


@dataclass
class Job:
    id: int
    payload: object
    state: str = "pending"  # pending, running, done
    attempts: int = 0
    available_at: float = 0.0
    lease_until: float | None = None
    worker: str | None = None


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

    def counts(self):
        """Number of jobs per state."""
        result = {}
        for job in self._jobs.values():
            result[job.state] = result.get(job.state, 0) + 1
        return result
