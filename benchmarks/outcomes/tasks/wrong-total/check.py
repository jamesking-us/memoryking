#!/usr/bin/env python3
"""Hidden check for wrong-total: run as `check.py WORKDIR`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import checklib  # noqa: E402

HIDDEN = '''
import inspect, random, unittest
from decimal import ROUND_HALF_EVEN, Decimal

from export import pipeline, report, rounding
from export.rates import RATES

CUR = list(RATES)


def make(seed, size):
    rng = random.Random(seed)
    out = []
    for _ in range(size):
        cur = rng.choice(CUR)
        amt = Decimal(rng.randint(1, 90000)) / (1 if cur == "JPY" else 1000)
        out.append({"amount": str(amt), "currency": cur, "day": "2024-05-01"})
    return out


def exact_total(txns):
    exact = sum((Decimal(t["amount"]) * RATES[t["currency"]] for t in txns), Decimal(0))
    return exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


class Hidden(unittest.TestCase):
    def test_large_accounts_match_the_books(self):
        for seed in range(1, 25):
            size = 90 + (seed * 37) % 400
            txns = make(seed, size)
            self.assertEqual(pipeline.account_total(txns), exact_total(txns), (seed, size))

    def test_batch_boundaries(self):
        for size in (99, 100, 101, 200, 201):
            txns = make(900 + size, size)
            self.assertEqual(pipeline.account_total(txns), exact_total(txns), size)

    def test_half_even_policy_kept(self):
        one = lambda a: [{"amount": a, "currency": "USD", "day": "d"}]
        self.assertEqual(pipeline.account_total(one("0.125")), Decimal("0.12"))
        self.assertEqual(pipeline.account_total(one("0.135")), Decimal("0.14"))
        self.assertEqual(rounding.money(Decimal("2.675")), Decimal("2.68"))
        self.assertEqual(rounding.money(Decimal("2.665")), Decimal("2.66"))

    def test_api_unchanged(self):
        self.assertEqual(list(inspect.signature(pipeline.account_total).parameters), ["txns", "rates"])
        rows = pipeline.export_rows({"b": make(1, 130), "a": make(2, 20)})
        self.assertEqual([r[0] for r in rows], ["a", "b"])
        self.assertTrue(report.render_csv(rows).startswith("account,total_usd\\na,"))

unittest.main()
'''


def main() -> int:
    workdir = checklib.workdir_arg()
    report = checklib.Report()
    ok, detail = checklib.hidden_tests(workdir, HIDDEN)
    report.add("hidden tests", ok, detail)
    return report.finish()


if __name__ == "__main__":
    raise SystemExit(main())
