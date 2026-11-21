import unittest

from recon.match import reconcile
from recon.models import Entry
from recon.summary import summarize


class MatchTests(unittest.TestCase):
    def test_simple_pairs(self):
        bank = [Entry("b1", 500, 10), Entry("b2", 700, 11)]
        books = [Entry("l1", 700, 11), Entry("l2", 500, 9)]
        result = reconcile(bank, books)
        self.assertEqual([(a.id, b.id) for a, b in result.pairs], [("b1", "l2"), ("b2", "l1")])
        self.assertEqual(result.unmatched_bank, [])
        self.assertEqual(result.unmatched_books, [])

    def test_outside_tolerance_is_unmatched(self):
        result = reconcile([Entry("b1", 500, 10)], [Entry("l1", 500, 13)])
        self.assertEqual(len(result.pairs), 0)
        self.assertEqual(len(result.unmatched_bank), 1)
        self.assertEqual(len(result.unmatched_books), 1)

    def test_each_entry_used_once(self):
        bank = [Entry("b1", 500, 10), Entry("b2", 500, 10)]
        books = [Entry("l1", 500, 10)]
        result = reconcile(bank, books)
        self.assertEqual(len(result.pairs), 1)
        self.assertEqual([e.id for e in result.unmatched_bank], ["b2"])

    def test_summary(self):
        result = reconcile([Entry("b1", 5, 1)], [Entry("l1", 5, 1)])
        self.assertTrue(summarize(result).startswith("1 matched"))


if __name__ == "__main__":
    unittest.main()
