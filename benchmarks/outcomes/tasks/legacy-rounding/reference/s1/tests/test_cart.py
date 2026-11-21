import unittest

from shop import cart


class CartTests(unittest.TestCase):
    def test_summary(self):
        items = [{"name": "tea", "price": 4.5, "qty": 2}, {"name": "cup", "price": 3.0, "qty": 1}]
        result = cart.summary(items, 0)
        self.assertEqual(result["lines"], [("tea", 2, 9.0), ("cup", 1, 3.0)])
        self.assertEqual(result["total"], 12.0)


if __name__ == "__main__":
    unittest.main()
