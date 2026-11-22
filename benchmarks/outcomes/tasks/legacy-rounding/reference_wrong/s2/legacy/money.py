"""Money helpers copied in from the payments platform."""

from decimal import ROUND_HALF_UP, Decimal


def to_cents(amount):
    """Whole cents for a dollar amount."""
    return int(round(float(amount) * 100))


def round_cents(amount):
    """Dollar amount rounded to two places."""
    return float(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def format_money(amount):
    return "$%.2f" % round_cents(amount)
