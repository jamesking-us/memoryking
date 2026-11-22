import unittest

from kvcache.cache import LRUCache


class CacheTests(unittest.TestCase):
    def test_get_put(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        self.assertEqual(cache.get("a"), 1)
        self.assertIsNone(cache.get("b"))
        self.assertEqual(cache.get("b", 7), 7)

    def test_evicts_least_recently_used(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.get("a")
        cache.put("c", 3)
        self.assertEqual(cache.keys(), ["a", "c"])

    def test_eviction_callback(self):
        seen = []
        cache = LRUCache(1, on_evict=lambda k, v: seen.append((k, v)))
        cache.put("a", 1)
        cache.put("b", 2)
        self.assertEqual(seen, [("a", 1)])

    def test_stats(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.get("a")
        cache.get("x")
        self.assertEqual(
            cache.stats(),
            {"size": 1, "capacity": 2, "hits": 1, "misses": 1, "evictions": 0},
        )

    def test_capacity_validated(self):
        with self.assertRaises(ValueError):
            LRUCache(0)


if __name__ == "__main__":
    unittest.main()
