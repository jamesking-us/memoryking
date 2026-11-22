"""Order pricing for the shop."""

from legacy.money import round_cents


def line_total(price, qty):
    """Price of one cart line."""
    return round_cents(float(price) * qty)


def subtotal(items):
    return sum(float(item["price"]) * item["qty"] for item in items)


def total(items, tax_rate):
    """Amount charged for items (dicts with price and qty) at the given tax rate."""
    base = subtotal(items)
    return round_cents(base + base * tax_rate)
