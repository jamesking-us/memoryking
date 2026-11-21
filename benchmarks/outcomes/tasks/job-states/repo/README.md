# jobq

In-memory job queue with worker leases.

    jobq/queue.py   Job, JobQueue (enqueue, get, counts)
    tests/          run with: python -m unittest discover -s tests

A job is `pending`, `running` or `done`. The queue takes its clock as a parameter so tests
can control time.
