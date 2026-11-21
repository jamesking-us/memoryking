import threading
import unittest

from kvcache.metrics import build_cache


class ThreadTests(unittest.TestCase):
    def test_concurrent_puts_and_gets(self):
        cache, log = build_cache(20)
        errors = []

        def work(seed):
            try:
                for i in range(2000):
                    key = (seed * 31 + i) % 80
                    cache.put(key, key)
                    cache.get(key)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(n,), daemon=True) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertLessEqual(len(cache), 20)
        self.assertEqual(cache.stats()["evictions"], len(log.entries))


if __name__ == "__main__":
    unittest.main()
