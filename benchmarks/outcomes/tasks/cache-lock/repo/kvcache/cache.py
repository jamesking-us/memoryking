"""A small least-recently-used cache."""

from collections import OrderedDict


class LRUCache:
    def __init__(self, capacity, on_evict=None):
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.capacity = capacity
        self._data = OrderedDict()
        self._on_evict = on_evict
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def get(self, key, default=None):
        if key in self._data:
            self._data.move_to_end(key)
            self.hits += 1
            return self._data[key]
        self.misses += 1
        return default

    def put(self, key, value):
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = value
        while len(self._data) > self.capacity:
            old_key, old_value = self._data.popitem(last=False)
            self.evictions += 1
            if self._on_evict is not None:
                self._on_evict(old_key, old_value)

    def __len__(self):
        return len(self._data)

    def __contains__(self, key):
        return key in self._data

    def keys(self):
        """Keys from least to most recently used."""
        return list(self._data)

    def stats(self):
        return {
            "size": len(self._data),
            "capacity": self.capacity,
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
        }
