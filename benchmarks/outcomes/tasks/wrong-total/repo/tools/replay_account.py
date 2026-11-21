#!/usr/bin/env python3
"""Replay a sample of production accounts through the export and compare with the books.

The sample (data/prod_sample.json) holds each account's transactions and the total the
books show, unrounded. Replaying every account takes a few minutes on the full sample.

    python tools/replay_account.py                  # as the code is today
    python tools/replay_account.py --rounding half_up
    python tools/replay_account.py --batch-size 100000
"""

import argparse
import json
import sys
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from export import pipeline, rounding  # noqa: E402

MODES = {"half_even": ROUND_HALF_EVEN, "half_up": ROUND_HALF_UP}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rounding", choices=sorted(MODES), default="half_even")
    parser.add_argument("--batch-size", type=int, default=pipeline.BATCH)
    parser.add_argument("--sample", default=str(Path(__file__).resolve().parents[1] / "data" / "prod_sample.json"))
    args = parser.parse_args()
    rounding.MODE = MODES[args.rounding]
    pipeline.BATCH = args.batch_size
    pipeline.batches.__defaults__ = (args.batch_size,)
    sample = json.loads(Path(args.sample).read_text())
    worst = Decimal(0)
    for name, entry in sorted(sample["accounts"].items()):
        got = pipeline.account_total(entry["txns"])
        want = rounding.money(Decimal(entry["books_total"]))
        diff = got - want
        worst = max(worst, abs(diff))
        print(f"{name:10} txns={len(entry['txns']):4} got={got} books={want} diff={diff}")
    print("worst difference:", worst)
    return 0 if worst == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
