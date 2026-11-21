import unittest

from ingest.codes import normalize_currency


class CodeTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_currency(" usd "), "USD")
        self.assertEqual(normalize_currency("US$"), "USD")
        self.assertEqual(normalize_currency("euro"), "EUR")
        self.assertEqual(normalize_currency("ukp"), "GBP")

    def test_invalid(self):
        for bad in ("", "US", "EURO1", "12$"):
            with self.assertRaises(ValueError):
                normalize_currency(bad)


if __name__ == "__main__":
    unittest.main()
