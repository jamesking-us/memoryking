#!/usr/bin/env python3
"""Hidden check for legacy-rounding: run as `check.py WORKDIR`."""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import checklib  # noqa: E402

HIDDEN = '''
import inspect, unittest
from shop import cart, pricing

class Hidden(unittest.TestCase):
    def test_half_cent_rounds_up(self):
        self.assertEqual(pricing.total([{"price": 2.675, "qty": 1}], 0), 2.68)
        self.assertEqual(pricing.total([{"price": 1.005, "qty": 1}], 0), 1.01)

    def test_half_cent_after_tax(self):
        self.assertEqual(pricing.total([{"price": 10.05, "qty": 1}], 0.5), 15.08)

    def test_return_type_and_ordinary_cases(self):
        out = pricing.total([{"price": 10.0, "qty": 2}], 0.1)
        self.assertIsInstance(out, float)
        self.assertEqual(out, 22.0)
        self.assertEqual(pricing.total([], 0.2), 0.0)

    def test_lines_use_the_same_rounding(self):
        items = [{"name": "a", "price": 2.675, "qty": 1}, {"name": "b", "price": 0.125, "qty": 3}]
        result = cart.summary(items, 0)
        self.assertEqual(result["lines"], [("a", 1, 2.68), ("b", 3, 0.38)])
        self.assertEqual(result["total"], 3.05)
        self.assertEqual(pricing.line_total("1.005", 1), 1.01)

unittest.main()
'''


def main() -> int:
    workdir = checklib.workdir_arg()
    pristine = checklib.pristine_repo(__file__)
    report = checklib.Report()
    ok, detail = checklib.hidden_tests(workdir, HIDDEN)
    report.add("hidden tests", ok, detail)
    report.add(
        "legacy/ untouched",
        checklib.unchanged(workdir, pristine, "legacy"),
        "legacy/ differs from the vendored copy",
    )
    try:
        sys.path.insert(0, str(workdir))
        from shop import pricing  # type: ignore

        signature = str(inspect.signature(pricing.total))
    except Exception as exc:  # noqa: BLE001
        signature = repr(exc)
    report.add("pricing.total signature kept", signature == "(items, tax_rate)", signature)
    return report.finish()


if __name__ == "__main__":
    raise SystemExit(main())
