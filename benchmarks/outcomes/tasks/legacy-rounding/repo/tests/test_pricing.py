import unittest

from shop import pricing


class PricingTests(unittest.TestCase):
    def test_total_with_tax(self):
        self.assertEqual(pricing.total([{"price": 10.0, "qty": 2}], 0.1), 22.0)

    def test_total_without_tax(self):
        self.assertEqual(pricing.total([{"price": 1.25, "qty": 3}], 0), 3.75)

    def test_line_total(self):
        self.assertEqual(pricing.line_total("4.50", 2), 9.0)

    def test_empty_cart(self):
        self.assertEqual(pricing.total([], 0.2), 0.0)


if __name__ == "__main__":
    unittest.main()
