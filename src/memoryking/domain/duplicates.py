"""When two quoted passages are the same fact recorded twice; deterministic and conservative.

Two observation excerpts are duplicates when, and only when:

* they are the same after case and whitespace are normalised; or
* each holds at least eight distinct words, they share at least 80% of their combined
  distinct words, they use the same negation words (not, no, never, ...), and every word
  that one has and the other lacks is a number, at most two per side ("failed again on CI
  run 3" and "... run 4").

Either way they must also name the same anchors: the same ticket ids, paths, symbols,
hosts and error codes. Two notes about PAY-142 and PAY-143, or about different files, are
never duplicates, however alike the rest of the sentence is. A differing word that is not a
number (another entity, another value, "thirty" against "sixty") keeps two notes apart, and
short notes are compared exactly.

`contained` is the other repeat: one excerpt that lies inside another, such as the user's own
turns that a hook captured as an observation of their own and again as part of the session
capture. It needs the shorter text to hold at least eight distinct words and at least 90% of
its text, paragraph by paragraph, to be found verbatim in the longer one, and to name no anchor
the longer one lacks.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from typing import Any

from memoryking.domain.anchors import extract
from memoryking.domain.lexicon import analyze

NEGATIONS = frozenset({"not", "no", "never", "none", "nor", "without", "cannot", "t"})
MINIMUM_WORDS = 8
OVERLAP = 0.8
DIFFERENCE = 2
COVERAGE = 0.9  # share of the shorter excerpt that must be found in the longer one


class Shape:
    """What a comparison needs from one excerpt; words and anchors are worked out when a
    comparison first needs them, because most pairs differ in length or text already."""

    __slots__ = ("text", "normalized", "_words", "_anchors", "_blocks")

    def __init__(self, text: str):
        self.text = text
        self.normalized = " ".join(text.casefold().split())
        self._blocks: list[str] | None = None
        self._words: frozenset[str] | None = None
        self._anchors: frozenset[tuple[str, str]] | None = None

    @property
    def words(self) -> frozenset[str]:
        if self._words is None:
            self._words = frozenset(token for token, _ in analyze(self.text))
        return self._words

    @property
    def blocks(self) -> list[str]:
        """The paragraphs (separated by blank lines), each normalised like `normalized`."""
        if self._blocks is None:
            parts = re.split(r"\n[ \t]*\n", self.text)
            self._blocks = [b for b in (" ".join(p.casefold().split()) for p in parts) if b]
        return self._blocks

    @property
    def anchors(self) -> frozenset[tuple[str, str]]:
        if self._anchors is None:
            self._anchors = frozenset(extract(self.text))
        return self._anchors


def shape(text: str) -> Shape:
    return Shape(text)


def duplicates(a: Shape, b: Shape) -> bool:
    if a.normalized == b.normalized:
        return a.anchors == b.anchors
    longer, shorter = (
        max(len(a.normalized), len(b.normalized)),
        min(len(a.normalized), len(b.normalized)),
    )
    if shorter < 2 * MINIMUM_WORDS or longer > shorter * 1.25 + 8:
        return False  # too short to compare loosely, or too different in length
    if min(len(a.words), len(b.words)) < MINIMUM_WORDS:
        return False
    only_a, only_b = a.words - b.words, b.words - a.words
    if (
        len(only_a) > DIFFERENCE
        or len(only_b) > DIFFERENCE
        or not all(word.isdigit() for word in only_a | only_b)
        or len(a.words & b.words) / len(a.words | b.words) < OVERLAP
    ):
        return False
    return a.anchors == b.anchors and a.words & NEGATIONS == b.words & NEGATIONS


def contained(small: Shape, large: Shape) -> bool:
    """`small`'s excerpt lies inside `large`'s: nearly all of it (90%, by paragraphs) is found
    verbatim in the longer one, which also names every anchor it names."""
    if len(small.normalized) >= len(large.normalized):
        return False
    allowed = (1 - COVERAGE) * len(small.normalized)  # text that may be missing
    for block in small.blocks:
        if block not in large.normalized:
            allowed -= len(block)
            if allowed < 0:
                return False
    return len(small.words) >= MINIMUM_WORDS and small.anchors <= large.anchors


def duplicate_clusters(shapes: list[Shape], keys: list[Any]) -> list[list[int]]:
    """Group the excerpts into clusters of duplicates, in order of first appearance: an excerpt
    joins the first cluster whose first member it duplicates (and whose `keys` entry equals its
    own, so a copy in a time range a question names stays apart). The same result as comparing
    every excerpt with every cluster's first member, without most of the comparisons: two
    excerpts can only be duplicates when their words other than numbers are the same, so only
    clusters whose first member has those words are compared."""
    clusters: list[list[int]] = []
    buckets: dict[tuple[Any, frozenset[str]], list[int]] = {}
    for index, mine in enumerate(shapes):
        key = (keys[index], frozenset(w for w in mine.words if not w.isdigit()))
        for number in buckets.get(key, ()):
            if duplicates(shapes[clusters[number][0]], mine):
                clusters[number].append(index)
                break
        else:
            clusters.append([index])
            buckets.setdefault(key, []).append(len(clusters) - 1)
    return clusters


def holders(shapes: list[Shape], keys: list[Any]) -> dict[int, int]:
    """For each excerpt that lies inside another (see `contained`; the other has the same `keys`
    entry), the shortest such other excerpt, the first of equally long ones. Only longer
    excerpts are compared, and an excerpt whose longest paragraph is more than a tenth of it
    must have that paragraph found word for word in the holder."""
    order = sorted(range(len(shapes)), key=lambda i: len(shapes[i].normalized))
    lengths = [len(shapes[i].normalized) for i in order]
    found: dict[int, int] = {}
    for index, small in enumerate(shapes):
        size = len(small.normalized)
        if size < 2 * MINIMUM_WORDS - 1:
            continue  # no room for eight distinct words
        longest = max(small.blocks, key=len, default="")
        must = longest if len(longest) > (1 - COVERAGE) * size else ""
        best: tuple[int, int] | None = None
        for position in range(bisect_right(lengths, size), len(order)):
            other = order[position]
            if keys[other] != keys[index]:
                continue
            big = shapes[other]
            if (must and must not in big.normalized) or not contained(small, big):
                continue
            if best is None or (len(big.text), other) < best:
                best = (len(big.text), other)
        if best is not None:
            found[index] = best[1]
    return found


_VERSION = re.compile(r"^(.+?)__v[0-9]+$")


def family(principal: str, source: str, source_event: str) -> tuple[str, str, str]:
    """The file a memory-tool or vault version belongs to: edits are new captures named
    `<name>__v<n>`, so every version of one note shares (source, name)."""
    match = _VERSION.match(source_event)
    return principal, source, match.group(1) if match else source_event
