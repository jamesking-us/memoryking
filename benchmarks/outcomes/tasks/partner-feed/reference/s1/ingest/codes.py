"""Currency code handling."""

ALIASES = {"US$": "USD", "EURO": "EUR", "UKP": "GBP"}


def normalize_currency(code):
    code = code.strip().upper()
    code = ALIASES.get(code, code)
    if len(code) != 3 or not code.isalpha():
        raise ValueError(f"not a currency code: {code!r}")
    return code
