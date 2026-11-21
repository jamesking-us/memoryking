"""Parser for the partner's daily feed."""

import csv
import datetime
import io
from decimal import Decimal

from ingest.codes import normalize_currency
from ingest.models import Feed, FeedError


def parse_feed(text):
    reader = csv.reader(io.StringIO(text))
    rows, skipped = [], 0
    for index, record in enumerate(reader):
        if index == 0 and record and record[0].startswith("#"):
            continue
        try:
            ident, date, amount, currency = record[:4]
            rows.append(
                {
                    "id": ident,
                    "date": datetime.date.fromisoformat(date),
                    "amount": Decimal(amount),
                    "currency": normalize_currency(currency),
                }
            )
        except (ValueError, TypeError):
            skipped += 1
    if not rows and skipped:
        raise FeedError("nothing usable in the feed")
    return Feed(rows, skipped)
