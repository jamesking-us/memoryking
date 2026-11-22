"""Pair bank statement lines with ledger entries."""

from collections import defaultdict

from recon.models import Result


def reconcile(bank, books, tolerance=1):
    """Pair each bank line with at most one ledger entry.

    A pair needs equal amounts and days at most `tolerance` apart. Bank lines are taken in
    the order given; each one is paired with the free ledger entry of the same amount whose
    day is closest. Returns Result(pairs, unmatched_bank, unmatched_books).
    """
    free = defaultdict(list)
    for index, entry in enumerate(books):
        free[entry.amount_cents].append(index)
    used = [False] * len(books)
    pairs, unmatched_bank = [], []
    for line in bank:
        candidates = free.get(line.amount_cents, [])
        fitting = [i for i in candidates if abs(books[i].day - line.day) <= tolerance]
        if not fitting:
            unmatched_bank.append(line)
            continue
        best = min(fitting, key=lambda i: (abs(books[i].day - line.day), i))
        candidates.remove(best)
        used[best] = True
        pairs.append((line, books[best]))
    unmatched_books = [entry for index, entry in enumerate(books) if not used[index]]
    return Result(pairs, unmatched_bank, unmatched_books)
