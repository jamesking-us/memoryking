import random
import time
import unittest

from recon.match import reconcile
from recon.models import Entry


class ScaleTests(unittest.TestCase):
    def test_first_listed_entry_wins_and_volume_is_fast(self):
        # Two free entries fit; the one listed first is taken even though the other is closer.
        result = reconcile([Entry("b", 100, 5)], [Entry("l1", 100, 6), Entry("l2", 100, 5)])
        self.assertEqual(result.pairs[0][1].id, "l1")
        rng = random.Random(1)
        books = [Entry(f"l{i}", rng.randrange(300) * 100, rng.randint(1, 28)) for i in range(20000)]
        bank = [Entry(f"b{i}", e.amount_cents, e.day) for i, e in enumerate(books)]
        started = time.time()
        reconcile(bank, books)
        self.assertLess(time.time() - started, 5)


if __name__ == "__main__":
    unittest.main()
