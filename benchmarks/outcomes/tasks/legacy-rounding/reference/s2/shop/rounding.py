"""Half-up rounding to cents on exact decimal arithmetic."""

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def dec(value):
    return Decimal(str(value))


def cents(value):
    """Round a Decimal to two places, halves up, and return a float dollar amount."""
    return float(value.quantize(CENT, rounding=ROUND_HALF_UP))
