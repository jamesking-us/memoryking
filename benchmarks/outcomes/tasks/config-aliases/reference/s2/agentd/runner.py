"""Runs jobs with the configured timeout and retry budget."""

import time


class JobTimeout(Exception):
    pass


class Runner:
    def __init__(self, settings, clock=time.monotonic):
        self.settings = settings
        self.clock = clock

    def run(self, job):
        """Call job(deadline) until it returns; retry JobTimeout up to max_retries times."""
        attempts = 0
        while True:
            deadline = self.clock() + self.settings.timeout_seconds
            try:
                return job(deadline)
            except JobTimeout:
                attempts += 1
                if attempts > self.settings.max_retries:
                    raise

    def describe(self):
        return "workers=%d timeout=%ss retries=%d" % (
            self.settings.workers,
            self.settings.timeout_seconds,
            self.settings.max_retries,
        )
