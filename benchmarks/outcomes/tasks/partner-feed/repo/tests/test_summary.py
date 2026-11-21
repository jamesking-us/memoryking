import datetime
import unittest
from decimal import Decimal

from ingest import summary
from ingest.models import Feed
from ingest.store import Ledger


def row(id, amount, currency, month):
    return {"id": id, "date": datetime.date(2024, month, 1), "amount": Decimal(amount), "currency": currency}


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger()
        self.ledger.add_feed(
            Feed([row("a", "1.50", "USD", 1), row("b", "2.25", "USD", 2), row("c", "9", "EUR", 2)], 0)
        )

    def test_by_currency(self):
        self.assertEqual(summary.by_currency(self.ledger), {"EUR": Decimal("9"), "USD": Decimal("3.75")})

    def test_by_month(self):
        self.assertEqual(summary.by_month(self.ledger), {(2024, 1): 1, (2024, 2): 2})

    def test_render(self):
        self.assertEqual(summary.render(self.ledger), "EUR: 9\nUSD: 3.75\nrows: 3")


if __name__ == "__main__":
    unittest.main()
