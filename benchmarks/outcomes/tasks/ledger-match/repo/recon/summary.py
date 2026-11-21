"""One-line summaries of a reconciliation."""


def summarize(result):
    matched = len(result.pairs)
    total = matched + len(result.unmatched_bank)
    rate = 100.0 * matched / total if total else 100.0
    return "%d matched, %d bank lines open, %d ledger entries open (%.1f%% of bank matched)" % (
        matched,
        len(result.unmatched_bank),
        len(result.unmatched_books),
        rate,
    )
