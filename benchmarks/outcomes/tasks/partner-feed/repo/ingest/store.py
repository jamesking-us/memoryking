"""Collecting parsed rows into an in-memory ledger."""

from decimal import Decimal


class Ledger:
    def __init__(self):
        self._rows = {}

    def add_feed(self, feed):
        """Add rows, newest wins on a repeated id. Returns the number of new ids."""
        added = 0
        for row in feed.rows:
            if row["id"] not in self._rows:
                added += 1
            self._rows[row["id"]] = row
        return added

    def total(self, currency):
        return sum((r["amount"] for r in self._rows.values() if r["currency"] == currency), Decimal(0))

    def __len__(self):
        return len(self._rows)
