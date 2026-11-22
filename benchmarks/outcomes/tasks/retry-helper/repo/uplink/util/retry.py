"""Retry helper that reports every call to the ops dashboard counters."""

import time

from uplink.net.errors import TransientError

STATS = {}


def reset_stats():
    STATS.clear()


def call_with_retry(fn, name, attempts=3, delay=0.2, sleep=None):
    """Call fn() and retry on TransientError, up to `attempts` calls in total.

    `name` labels the operation in STATS, which feeds the dashboard counters.
    """
    sleep = sleep or time.sleep
    entry = STATS.setdefault(name, {"calls": 0, "retries": 0})
    entry["calls"] += 1
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except TransientError:
            if attempt == attempts:
                raise
            entry["retries"] += 1
            sleep(delay * attempt)
