"""Pair bank statement lines with ledger entries."""

from recon.models import Result


def reconcile(bank, books, tolerance=1):
    """Pair each bank line with at most one ledger entry.

    A pair needs equal amounts and days at most `tolerance` apart. Bank lines are taken in
    the order given; each one claims the first ledger entry, in ledger order, that is still
    free and fits. Returns Result(pairs, unmatched_bank, unmatched_books), where the
    unmatched lists keep their original order and pairs follow the bank order.
    """
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
    unmatched_books = [entry for index, entry in enumerate(books) if not used[index]]
    return Result(pairs, unmatched_bank, unmatched_books)
