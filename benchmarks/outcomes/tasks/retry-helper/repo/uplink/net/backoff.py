"""Exponential backoff decorator.

    @backoff(max_tries=5)
    def call(): ...
"""

import functools
import time

from uplink.net.errors import TransientError


def backoff(max_tries=5, base=0.1, factor=2.0):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            wait = base
            for attempt in range(1, max_tries + 1):
                try:
                    return fn(*args, **kwargs)
                except TransientError:
                    if attempt == max_tries:
                        raise
                    time.sleep(wait)
                    wait *= factor

        return wrapper

    return decorate
