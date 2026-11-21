"""Types shared by the ingest code."""

from collections import namedtuple

# A parsed feed: `rows` is a list of dicts (id, date, amount, currency) and `skipped` counts
# the rows that were dropped as corrupt.
Feed = namedtuple("Feed", "rows skipped")


class FeedError(Exception):
    """The feed as a whole cannot be used."""
