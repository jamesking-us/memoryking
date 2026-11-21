import unittest

from shop.inventory import Inventory, OutOfStock
from shop.receipt import render


class InventoryTests(unittest.TestCase):
    def test_reserve_all_is_atomic(self):
        inv = Inventory({"a": 2, "b": 1})
        with self.assertRaises(OutOfStock):
            inv.reserve_all([{"sku": "a", "qty": 2}, {"sku": "b", "qty": 5}])
        self.assertEqual(inv.available("a"), 2)

    def test_receipt_lists_total(self):
        text = render([("tea", 2, 9.0)], 9.0)
        self.assertIn("TOTAL", text)
        self.assertIn("$9.00", text)


if __name__ == "__main__":
    unittest.main()
