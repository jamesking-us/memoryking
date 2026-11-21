#!/usr/bin/env python3
"""Hidden check for cache-lock: run as `check.py WORKDIR`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import checklib  # noqa: E402

HIDDEN = '''
import random, sys, threading, time, unittest

from kvcache.cache import LRUCache
from kvcache.metrics import build_cache


class SlowKey(int):
    """Hashing yields the interpreter, so threads interleave inside every cache operation."""

    def __hash__(self):
        time.sleep(0.0001)
        return int.__hash__(self)


def hammer(cache, threads=8, ops=500, keyspace=200, seed=0):
    errors, gets = [], []
    sys.setswitchinterval(1e-5)

    def work(n):
        rng = random.Random(seed * 100 + n)
        count = 0
        try:
            for _ in range(ops):
                key = SlowKey(rng.randrange(keyspace))
                if rng.random() < 0.5:
                    cache.put(key, key * 2)
                else:
                    count += 1
                    value = cache.get(key)
                    if value is not None and value != key * 2:
                        raise AssertionError("wrong value")
        except Exception as exc:
            errors.append(repr(exc))
        gets.append(count)

    pool = [threading.Thread(target=work, args=(i,), daemon=True) for i in range(threads)]
    for t in pool:
        t.start()
    end = time.time() + 20
    for t in pool:
        t.join(timeout=max(0.1, end - time.time()))
    hung = [t for t in pool if t.is_alive()]
    return errors, sum(gets), hung


class Hidden(unittest.TestCase):
    def test_single_thread_behaviour(self):
        cache = LRUCache(2)
        cache.put("a", 1); cache.put("b", 2); cache.get("a"); cache.put("c", 3)
        self.assertEqual(cache.keys(), ["a", "c"])
        self.assertIn("a", cache)
        self.assertEqual(len(cache), 2)
        self.assertEqual(cache.stats()["evictions"], 1)

    def test_service_cache_under_threads(self):
        for seed in (1, 2, 3):
            cache, log = build_cache(50)
            errors, gets, hung = hammer(cache, seed=seed)
            self.assertFalse(hung, "threads did not finish: deadlock")
            self.assertEqual(errors, [])
            stats = cache.stats()
            self.assertLessEqual(len(cache), 50)
            self.assertEqual(stats["hits"] + stats["misses"], gets)
            self.assertEqual(stats["evictions"], len(log.entries))
            self.assertTrue(all(size is not None and size <= 50 for _, size in log.entries))

    def test_plain_callback_cache_under_threads(self):
        seen = []
        cache = LRUCache(30, on_evict=lambda k, v: seen.append(k))
        errors, gets, hung = hammer(cache, seed=9)
        self.assertFalse(hung)
        self.assertEqual(errors, [])
        self.assertEqual(cache.stats()["evictions"], len(seen))

    def test_callback_may_use_the_cache(self):
        box = {}
        cache = LRUCache(1, on_evict=lambda k, v: box.setdefault("len", len(box["cache"])))
        box["cache"] = cache
        done = threading.Event()

        def run():
            cache.put("a", 1); cache.put("b", 2)
            done.set()

        threading.Thread(target=run, daemon=True).start()
        self.assertTrue(done.wait(5), "callback re-entering the cache deadlocked")

unittest.main()
'''


def main() -> int:
    workdir = checklib.workdir_arg()
    report = checklib.Report()
    ok, detail = checklib.hidden_tests(workdir, HIDDEN, timeout=120)
    report.add("hidden tests", ok, detail)
    return report.finish()


if __name__ == "__main__":
    raise SystemExit(main())
