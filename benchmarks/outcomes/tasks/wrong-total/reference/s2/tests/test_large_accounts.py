import random
import unittest
from decimal import Decimal

from export import pipeline
from export.rates import RATES


class LargeAccountTests(unittest.TestCase):
    def test_total_is_the_rounded_exact_sum(self):
        rng = random.Random(3)
        for size in (150, 420):
            txns = [
                {"amount": str(Decimal(rng.randint(1, 90000)) / 1000), "currency": "EUR", "day": "d"}
                for _ in range(size)
            ]
            exact = sum((Decimal(t["amount"]) * RATES["EUR"] for t in txns), Decimal(0))
            self.assertEqual(
                pipeline.account_total(txns), exact.quantize(Decimal("0.01"))
            )


if __name__ == "__main__":
    unittest.main()
