"""Summaries of what a ledger holds."""

from collections import defaultdict
from decimal import Decimal


def by_currency(ledger):
    """Totals per currency, sorted by currency code."""
    totals = defaultdict(Decimal)
    for row in ledger._rows.values():
        totals[row["currency"]] += row["amount"]
    return dict(sorted(totals.items()))


def by_month(ledger):
    """Row counts per (year, month), sorted."""
    counts = defaultdict(int)
    for row in ledger._rows.values():
        counts[(row["date"].year, row["date"].month)] += 1
    return dict(sorted(counts.items()))


def render(ledger):
    lines = [f"{code}: {total}" for code, total in by_currency(ledger).items()]
    lines.append("rows: %d" % len(ledger))
    return "\n".join(lines)
