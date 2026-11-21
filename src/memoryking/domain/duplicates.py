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
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from memoryking.domain.anchors import extract
from memoryking.domain.lexicon import analyze

NEGATIONS = frozenset({"not", "no", "never", "none", "nor", "without", "cannot", "t"})
MINIMUM_WORDS = 8
OVERLAP = 0.8
DIFFERENCE = 2


@dataclass(frozen=True)
class Shape:
    """What a comparison needs from one excerpt."""

    normalized: str
    words: frozenset[str]
    negations: frozenset[str]
    anchors: frozenset[tuple[str, str]]


def shape(text: str) -> Shape:
    words = frozenset(token for token, _ in analyze(text))
    return Shape(
        " ".join(text.casefold().split()),
        words,
        words & NEGATIONS,
        frozenset(extract(text)),
    )


def duplicates(a: Shape, b: Shape) -> bool:
    if a.anchors != b.anchors:
        return False
    if a.normalized == b.normalized:
        return True
    if min(len(a.words), len(b.words)) < MINIMUM_WORDS or a.negations != b.negations:
        return False
    only_a, only_b = a.words - b.words, b.words - a.words
    return (
        len(a.words & b.words) / len(a.words | b.words) >= OVERLAP
        and len(only_a) <= DIFFERENCE
        and len(only_b) <= DIFFERENCE
        and all(word.isdigit() for word in only_a | only_b)
    )


_VERSION = re.compile(r"^(.+?)__v[0-9]+$")


def family(principal: str, source: str, source_event: str) -> tuple[str, str, str]:
    """The file a memory-tool or vault version belongs to: edits are new captures named
    `<name>__v<n>`, so every version of one note shares (source, name)."""
    match = _VERSION.match(source_event)
    return principal, source, match.group(1) if match else source_event
