#!/usr/bin/env python3
"""Hidden check for ledger-match: run as `check.py WORKDIR`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import checklib  # noqa: E402

HIDDEN = '''
import random, time, unittest

from recon.match import reconcile
from recon.models import Entry, Result


def original(bank, books, tolerance=1):
    used = [False] * len(books)
    pairs, unmatched_bank = [], []
    for line in bank:
        for index, entry in enumerate(books):
            if used[index]:
                continue
            if entry.amount_cents == line.amount_cents and abs(entry.day - line.day) <= tolerance:
                used[index] = True
                pairs.append((line, entry))
                break
        else:
            unmatched_bank.append(line)
    return Result(pairs, unmatched_bank, [e for i, e in enumerate(books) if not used[i]])


def make(seed, n_books, n_bank, distinct):
    rng = random.Random(seed)
    amounts = [rng.randint(100, 90000) for _ in range(distinct)]
    books = [Entry(f"L{i}", rng.choice(amounts), rng.randint(1, 28)) for i in range(n_books)]
    bank = [
        Entry(f"B{i}", rng.choice(amounts), rng.randint(1, 28)) for i in range(n_bank)
    ]
    return bank, books


class Hidden(unittest.TestCase):
    def test_same_pairs_as_the_current_matcher(self):
        for seed, (nb, nk, distinct) in enumerate(
            [(300, 300, 8), (800, 700, 40), (1500, 1600, 100), (50, 50, 3), (400, 120, 5)]
        ):
            bank, books = make(seed, nk, nb, distinct)
            for tol in (0, 1, 2):
                want, got = original(bank, books, tol), reconcile(bank, books, tol)
                self.assertEqual(list(got.pairs), want.pairs, (seed, tol))
                self.assertEqual(list(got.unmatched_bank), want.unmatched_bank, (seed, tol))
                self.assertEqual(list(got.unmatched_books), want.unmatched_books, (seed, tol))

    def test_first_listed_entry_wins(self):
        r = reconcile([Entry("b", 100, 5)], [Entry("l1", 100, 6), Entry("l2", 100, 5)])
        self.assertEqual(r.pairs[0][1].id, "l1")

    def test_month_end_volume(self):
        bank, books = make(99, 20000, 20000, 300)
        import signal

        def too_slow(signum, frame):
            raise AssertionError("20,000 lines took more than 4 seconds")

        signal.signal(signal.SIGALRM, too_slow)
        signal.alarm(4)
        started = time.time()
        result = reconcile(bank, books)
        signal.alarm(0)
        elapsed = time.time() - started
        self.assertLess(elapsed, 4, f"took {elapsed:.1f}s")
        self.assertEqual(len(result.pairs) + len(result.unmatched_bank), 20000)

unittest.main()
'''


def main() -> int:
    workdir = checklib.workdir_arg()
    report = checklib.Report()
    ok, detail = checklib.hidden_tests(workdir, HIDDEN, timeout=240)
    report.add("hidden tests", ok, detail)
    return report.finish()


if __name__ == "__main__":
    raise SystemExit(main())
