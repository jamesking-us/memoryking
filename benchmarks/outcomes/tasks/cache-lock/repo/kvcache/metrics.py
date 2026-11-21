"""Eviction bookkeeping and the cache the service actually uses."""

from kvcache.cache import LRUCache


class EvictionLog:
    """Remembers each evicted key together with the cache size at that moment."""

    def __init__(self):
        self.cache = None
        self.entries = []

    def record(self, key, value):
        size = self.cache.stats()["size"] if self.cache is not None else None
        self.entries.append((key, size))


def build_cache(capacity=128):
    """The service's cache and the log that watches its evictions."""
    log = EvictionLog()
    cache = LRUCache(capacity, on_evict=log.record)
    log.cache = cache
    return cache, log
