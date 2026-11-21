"""Stock levels for the storefront."""


class OutOfStock(Exception):
    pass


class Inventory:
    def __init__(self, stock=None):
        self._stock = dict(stock or {})

    def available(self, sku):
        return self._stock.get(sku, 0)

    def reserve(self, sku, qty):
        if qty <= 0:
            raise ValueError("qty must be positive")
        if self.available(sku) < qty:
            raise OutOfStock(sku)
        self._stock[sku] -= qty

    def restock(self, sku, qty):
        self._stock[sku] = self.available(sku) + qty

    def reserve_all(self, items):
        """Reserve every item or none of them."""
        taken = []
        try:
            for item in items:
                self.reserve(item["sku"], item["qty"])
                taken.append(item)
        except OutOfStock:
            for item in taken:
                self.restock(item["sku"], item["qty"])
            raise
