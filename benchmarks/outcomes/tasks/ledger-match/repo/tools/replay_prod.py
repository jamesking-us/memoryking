#!/usr/bin/env python3
"""Replay last month's production statements through the matcher.

data/prod_month.json holds the bank lines, the ledger entries and the pairs the current
matcher produced for them (the pairs finance signed off). Prints how many pairs differ.

    python tools/replay_prod.py
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recon.match import reconcile  # noqa: E402
from recon.models import Entry  # noqa: E402


def main():
    data = json.loads((Path(__file__).resolve().parents[1] / "data" / "prod_month.json").read_text())
    bank = [Entry(*row) for row in data["bank"]]
    books = [Entry(*row) for row in data["books"]]
    started = time.time()
    result = reconcile(bank, books)
    took = time.time() - started
    got = [(a.id, b.id) for a, b in result.pairs]
    golden = [tuple(pair) for pair in data["pairs"]]
    differ = sum(1 for g, w in zip(got, golden) if g != w) + abs(len(got) - len(golden))
    print(f"{len(bank)} bank lines, {len(books)} ledger entries, {len(got)} pairs in {took:.2f}s")
    print(f"{differ} of {len(golden)} signed-off pairs differ")
    return 0 if differ == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
