"""Pair bank statement lines with ledger entries."""

from collections import defaultdict

from recon.models import Result


def reconcile(bank, books, tolerance=1):
    """Pair each bank line with at most one ledger entry.

    A pair needs equal amounts and days at most `tolerance` apart. Bank lines are taken in
    the order given; each one claims the first ledger entry, in ledger order, that is still
    free and fits. Returns Result(pairs, unmatched_bank, unmatched_books), where the
    unmatched lists keep their original order and pairs follow the bank order.
    """
    # Only entries with the same amount can pair, so keep each amount's free entries in
    # ledger order and scan just those.
    free = defaultdict(list)
    for index, entry in enumerate(books):
        free[entry.amount_cents].append(index)
    used = [False] * len(books)
    pairs, unmatched_bank = [], []
    for line in bank:
        candidates = free.get(line.amount_cents, ())
        for position, index in enumerate(candidates):
            if abs(books[index].day - line.day) <= tolerance:
                used[index] = True
                pairs.append((line, books[index]))
                del candidates[position]
                break
        else:
            unmatched_bank.append(line)
    unmatched_books = [entry for index, entry in enumerate(books) if not used[index]]
    return Result(pairs, unmatched_bank, unmatched_books)
