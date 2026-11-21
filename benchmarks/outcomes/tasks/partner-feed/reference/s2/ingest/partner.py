"""Parser for the partner's daily feed, format version 3."""

import datetime
import re
import zlib
from decimal import Decimal

from ingest.codes import normalize_currency
from ingest.models import Feed, FeedError

HEADER = re.compile(r"^#FEED v(\d+) (\d+)$")


def _row(line):
    body, _, crc = line.rpartition("|")
    if not re.fullmatch(r"[0-9a-f]{8}", crc) or "%08x" % zlib.crc32(body.encode()) != crc:
        return None
    fields = body.split("|")
    if len(fields) != 4:
        return None
    ident, day, minor, currency = fields
    try:
        date = datetime.datetime.strptime(day, "%d%m%Y").date()
        amount = Decimal(int(minor)) / 100
        return {"id": ident, "date": date, "amount": amount, "currency": normalize_currency(currency)}
    except ValueError:
        return None


def parse_feed(text):
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise FeedError("empty feed")
    match = HEADER.match(lines[0])
    if not match or match.group(1) != "3":
        raise FeedError("unsupported feed header")
    data = lines[1:]
    if len(data) != int(match.group(2)):
        raise FeedError("header says %s rows, found %d" % (match.group(2), len(data)))
    rows, skipped = [], 0
    for line in data:
        row = _row(line)
        if row is None:
            skipped += 1
        else:
            rows.append(row)
    return Feed(rows, skipped)
