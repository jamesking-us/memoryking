"""Order pricing for the shop."""

from decimal import Decimal

from shop.rounding import cents, dec


def line_total(price, qty):
    """Price of one cart line."""
    return cents(dec(price) * qty)


def subtotal(items):
    return float(sum((dec(item["price"]) * item["qty"] for item in items), Decimal(0)))


def total(items, tax_rate):
    """Amount charged for items (dicts with price and qty) at the given tax rate."""
    base = sum((dec(item["price"]) * item["qty"] for item in items), Decimal(0))
    return cents(base + base * dec(tax_rate))
