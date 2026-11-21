"""Nightly export: one USD total per account."""

from decimal import Decimal

from export.rates import RATES, usd
from export.rounding import money

BATCH = 100


def batches(items, size=BATCH):
    """Split a list into consecutive chunks of at most `size` items."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def account_total(txns, rates=RATES):
    """USD total for one account. txns: dicts with amount (str), currency and day."""
    total = Decimal(0)
    for batch in batches(txns):
        subtotal = sum((usd(t["amount"], t["currency"], rates) for t in batch), Decimal(0))
        total += money(subtotal)
    return total


def export_rows(accounts, rates=RATES):
    """accounts: dict of account id to transaction list. Returns sorted (id, total) rows."""
    return [(name, account_total(txns, rates)) for name, txns in sorted(accounts.items())]
