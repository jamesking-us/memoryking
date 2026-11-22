"""Plain-text receipts."""

from legacy.money import format_money


def render(lines, total):
    """lines: iterable of (name, qty, line_total)."""
    rows = ["%-20s x%-3d %10s" % (name, qty, format_money(amount)) for name, qty, amount in lines]
    rows.append("%-25s %10s" % ("TOTAL", format_money(total)))
    return "\n".join(rows)
