"""Currency rates, in USD per unit of currency."""

from decimal import Decimal

RATES = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.0837"),
    "GBP": Decimal("1.2641"),
    "JPY": Decimal("0.0067"),
}


def usd(amount, currency, rates=RATES):
    return Decimal(amount) * rates[currency]
