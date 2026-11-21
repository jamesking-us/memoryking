import datetime
import unittest
from decimal import Decimal

from ingest.models import Feed
from ingest.store import Ledger


def row(id, amount, currency="USD"):
    return {"id": id, "date": datetime.date(2024, 5, 1), "amount": Decimal(amount), "currency": currency}


class StoreTests(unittest.TestCase):
    def test_add_and_total(self):
        ledger = Ledger()
        self.assertEqual(ledger.add_feed(Feed([row("a", "1.50"), row("b", "2.00", "EUR")], 0)), 2)
        self.assertEqual(ledger.total("USD"), Decimal("1.50"))
        self.assertEqual(len(ledger), 2)

    def test_newest_wins(self):
        ledger = Ledger()
        ledger.add_feed(Feed([row("a", "1.00")], 0))
        self.assertEqual(ledger.add_feed(Feed([row("a", "3.00")], 0)), 0)
        self.assertEqual(ledger.total("USD"), Decimal("3.00"))


if __name__ == "__main__":
    unittest.main()
