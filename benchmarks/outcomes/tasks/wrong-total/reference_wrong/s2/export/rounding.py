"""Rounding for money values."""

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
MODE = ROUND_HALF_UP


def money(value):
    """Round a Decimal to whole cents."""
    return value.quantize(CENT, rounding=MODE)
