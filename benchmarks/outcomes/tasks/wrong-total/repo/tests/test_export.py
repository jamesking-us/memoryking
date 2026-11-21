import unittest
from decimal import Decimal

from export import pipeline, report
from export.rounding import money


def txn(amount, currency="USD"):
    return {"amount": amount, "currency": currency, "day": "2024-05-01"}


class ExportTests(unittest.TestCase):
    def test_small_account(self):
        total = pipeline.account_total([txn("10.00"), txn("5.50", "EUR")])
        self.assertEqual(total, Decimal("15.96"))

    def test_money_rounds_half_even(self):
        self.assertEqual(money(Decimal("0.125")), Decimal("0.12"))
        self.assertEqual(money(Decimal("0.135")), Decimal("0.14"))

    def test_batches(self):
        self.assertEqual([len(b) for b in pipeline.batches(list(range(250)))], [100, 100, 50])

    def test_rows_sorted_and_csv(self):
        rows = pipeline.export_rows({"b": [txn("1.00")], "a": [txn("2.00")]})
        self.assertEqual([r[0] for r in rows], ["a", "b"])
        self.assertEqual(report.render_csv(rows), "account,total_usd\na,2.00\nb,1.00\n")


if __name__ == "__main__":
    unittest.main()
