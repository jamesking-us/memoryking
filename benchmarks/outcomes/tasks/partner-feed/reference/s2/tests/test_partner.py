import datetime
import unittest
import zlib
from decimal import Decimal

from ingest.models import FeedError
from ingest.partner import parse_feed


def line(body):
    return "%s|%08x" % (body, zlib.crc32(body.encode()))


class PartnerTests(unittest.TestCase):
    def test_parse(self):
        text = "#FEED v3 2\n" + line("a1|05032024|12345|us$") + "\n" + line("a2|31122023|-50|EUR") + "\n"
        feed = parse_feed(text)
        self.assertEqual(feed.skipped, 0)
        self.assertEqual(feed.rows[0], {"id": "a1", "date": datetime.date(2024, 3, 5), "amount": Decimal("123.45"), "currency": "USD"})
        self.assertEqual(feed.rows[1]["amount"], Decimal("-0.50"))

    def test_bad_checksum_is_skipped(self):
        feed = parse_feed("#FEED v3 2\n" + line("a1|05032024|100|USD") + "\na2|05032024|100|USD|00000000\n")
        self.assertEqual((len(feed.rows), feed.skipped), (1, 1))

    def test_unusable_feeds(self):
        with self.assertRaises(FeedError):
            parse_feed("#FEED v2 0\n")
        with self.assertRaises(FeedError):
            parse_feed("#FEED v3 2\n" + line("a1|05032024|100|USD") + "\n")


if __name__ == "__main__":
    unittest.main()
