"""Money helpers copied in from the payments platform."""


def to_cents(amount):
    """Whole cents for a dollar amount."""
    return int(round(float(amount) * 100))


def round_cents(amount):
    """Dollar amount rounded to two places."""
    return round(float(amount), 2)


def format_money(amount):
    return "$%.2f" % round_cents(amount)
