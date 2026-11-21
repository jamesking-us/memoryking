#!/usr/bin/env python3
"""Hidden check for partner-feed: run as `check.py WORKDIR`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import checklib  # noqa: E402

HIDDEN = '''
import datetime, unittest, zlib
from decimal import Decimal

from ingest.codes import normalize_currency
from ingest.models import Feed, FeedError
from ingest.partner import parse_feed


def line(body):
    return "%s|%08x" % (body, zlib.crc32(body.encode()))


class Hidden(unittest.TestCase):
    def test_codes(self):
        self.assertEqual([normalize_currency(c) for c in (" usd", "US$", "Euro", "ukp", "jpy")],
                         ["USD", "USD", "EUR", "GBP", "JPY"])
        for bad in ("", "US", "EURO1", "12$"):
            with self.assertRaises(ValueError):
                normalize_currency(bad)

    def test_parse_good_feed(self):
        text = "\\n".join([
            "#FEED v3 4",
            line("tx-1|05032024|12345|usd"),
            line("tx-2|31122023|-50|EURO"),
            line("tx-3|01012024|0|gbp"),
            line("tx-4|29022024|100000000|JPY"),
        ]) + "\\n"
        feed = parse_feed(text)
        self.assertIsInstance(feed, Feed)
        self.assertEqual(feed.skipped, 0)
        self.assertEqual(
            [(r["id"], r["date"], r["amount"], r["currency"]) for r in feed.rows],
            [
                ("tx-1", datetime.date(2024, 3, 5), Decimal("123.45"), "USD"),
                ("tx-2", datetime.date(2023, 12, 31), Decimal("-0.50"), "EUR"),
                ("tx-3", datetime.date(2024, 1, 1), Decimal("0.00"), "GBP"),
                ("tx-4", datetime.date(2024, 2, 29), Decimal("1000000.00"), "JPY"),
            ],
        )
        self.assertIsInstance(feed.rows[0]["amount"], Decimal)

    def test_bad_checksums_are_dropped_and_counted(self):
        good = line("a|05032024|100|USD")
        bad_crc = "b|05032024|100|USD|deadbeef"
        upper = "c|05032024|100|USD|%08X" % zlib.crc32(b"c|05032024|100|USD")
        short = "d|05032024|100|USD|1234"
        feed = parse_feed("\\n".join(["#FEED v3 4", good, bad_crc, upper, short]))
        self.assertEqual([r["id"] for r in feed.rows], ["a"])
        self.assertEqual(feed.skipped, 3)

    def test_tampered_row_is_dropped(self):
        original = line("a|05032024|100|USD")
        tampered = original.replace("|100|", "|900|")
        feed = parse_feed("#FEED v3 1\\n" + tampered)
        self.assertEqual((feed.rows, feed.skipped), ([], 1))

    def test_unusable_feeds(self):
        row = line("a|05032024|100|USD")
        for text in (
            "#FEED v2 1\\n" + row,
            "#FEED v4 1\\n" + row,
            "#FEED v3 2\\n" + row,
            "#FEED v3 0\\n" + row,
            row,
            "",
        ):
            with self.assertRaises(FeedError, msg=text):
                parse_feed(text)

    def test_empty_feed_is_fine(self):
        feed = parse_feed("#FEED v3 0\\n")
        self.assertEqual((feed.rows, feed.skipped), ([], 0))

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
