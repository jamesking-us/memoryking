"""Cart summaries."""

from shop import pricing


def summary(items, tax_rate):
    lines = [
        (item["name"], item["qty"], pricing.line_total(item["price"], item["qty"]))
        for item in items
    ]
    return {"lines": lines, "total": pricing.total(items, tax_rate)}
