"""Records that were captured one after another, as one conversation: an episode.

A chat captured turn by turn is one record per turn, and nothing in the store says which turns
belong together. What it does know is the order records arrived in, who captured them and from
which source, and when each was observed. An *episode* of a record is the run of records of the
same project, source and actor around it in capture order, in which every record was observed
within `GAP_MS` of the one before. A briefing uses it to show the records beside a result, so
that an answer split over two turns is whole.

Reading an episode never widens what a reader may see. Every record is read through the same
filter as a search (project, not deleted, recorded by the briefing's position, the reader's own
audience, not held), skipped when it fails it, and then through the store's single visibility
check, which ends the run at a record the reader's grants do not cover. Only records the hit's
own source and actor wrote are taken: a record of another source or project is not part of the
run, so it can neither be shown nor shape which records are shown.
"""

from __future__ import annotations

from typing import Any

from memoryking.application.store_core import StoreCore

GAP_MS = 30 * 60 * 1000  # longest silence inside one episode
SPAN = 48  # records looked at on each side of a result

SELECT = (
    "SELECT o.* FROM observations o WHERE o.tenant=? AND o.project=? AND o.source=? "
    "AND o.principal=? AND o.deleted=0 AND o.position<=? "
    "AND (o.readers='null' OR EXISTS(SELECT 1 FROM json_each(o.readers) WHERE value=?)) "
    "AND NOT EXISTS(SELECT 1 FROM observation_holds h WHERE h.tenant=o.tenant AND h.id=o.id AND h.state='held') "
)
BEFORE = SELECT + "AND o.position<? ORDER BY o.position DESC LIMIT ?"
AFTER = SELECT + "AND o.position>? ORDER BY o.position ASC LIMIT ?"


class Episodes:
    """The episodes of the records one briefing looks at, read lazily and kept for its length."""

    def __init__(self, store: StoreCore, who: dict[str, Any], recorded: int, at: int):
        self.s, self.who, self.recorded, self.at = store, who, recorded, at
        self.runs: dict[
            str, list[Any]
        ] = {}  # record id -> its episode's visible rows, oldest first

    def of(self, row: Any) -> list[Any]:
        """The episode of `row` (a visible observation row) as rows in capture order, or just
        the row itself when nothing was captured beside it."""
        if row["id"] in self.runs:
            return self.runs[row["id"]]
        base = (
            row["tenant"],
            row["project"],
            row["source"],
            row["principal"],
            self.recorded,
            self.who["principal"],
        )
        before = self._reach(
            self.s.db.execute(BEFORE, (*base, row["position"], SPAN)).fetchall(), row
        )
        after = self._reach(
            self.s.db.execute(AFTER, (*base, row["position"], SPAN)).fetchall(), row
        )
        run = [*reversed(before), row, *after]
        for member in run:
            self.runs[member["id"]] = run
        return run

    def _reach(self, rows: list[Any], start: Any) -> list[Any]:
        """The rows from `start` outwards that are visible and unbroken by a silence."""
        kept: list[Any] = []
        last = start["observed_at_ms"]
        for candidate in rows:
            if abs(candidate["observed_at_ms"] - last) > GAP_MS:
                break
            if not self.s._visible(self.who, candidate, self.at):
                break  # a record the reader cannot see ends the run; nothing is read past it
            kept.append(candidate)
            last = candidate["observed_at_ms"]
        return kept

    def beside(self, row: Any, reach: int) -> list[Any]:
        """The records within `reach` places of `row` in its episode, nearest first, the one
        after before the one before at equal distance."""
        run = self.of(row)
        index = next(i for i, member in enumerate(run) if member["id"] == row["id"])
        found: list[Any] = []
        for distance in range(1, reach + 1):
            for place in (index + distance, index - distance):
                if 0 <= place < len(run):
                    found.append(run[place])
        return found
