"""Read-only views of a queue for dashboards."""


def depth(queue):
    """Jobs waiting to be leased right now."""
    now = queue.clock()
    return sum(1 for job in queue._jobs.values() if job.state == "pending" and job.available_at <= now)


def oldest_wait(queue):
    """Seconds the longest-waiting ready job has been ready, or 0 when none are."""
    now = queue.clock()
    waits = [now - job.available_at for job in queue._jobs.values() if job.state == "pending" and job.available_at <= now]
    return max(waits, default=0)


def by_worker(queue):
    """Number of running jobs per worker."""
    result = {}
    for job in queue._jobs.values():
        if job.state == "running":
            result[job.worker] = result.get(job.worker, 0) + 1
    return result
