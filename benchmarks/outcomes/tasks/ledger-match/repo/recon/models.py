from collections import namedtuple

# One line on a statement: an id, an amount in whole cents and a day number.
Entry = namedtuple("Entry", "id amount_cents day")

Result = namedtuple("Result", "pairs unmatched_bank unmatched_books")
