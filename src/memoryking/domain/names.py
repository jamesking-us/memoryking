"""Which listed records hold the words a query names together, and in that order.

A query that names a thing ("record identifiers", "default listen port of the gateway client
settings") matches records that hold the words apart as well as records that hold them side by
side, and in a long history the second kind outnumbers the first by a hundred to one. The words
of the query are only what was typed: nothing here knows what a record is about, reads how it
is worded, or knows any vocabulary. A record holds a *pair* when two neighbouring query words
that are not function words stand next to each other in its text, in the query's order (case,
punctuation and spacing do not matter). A pair that every listed record holds says nothing about
which is the one asked for and weighs nothing; a pair that few hold weighs most (the logarithm of
the share of records that lack it).
"""

from __future__ import annotations

import math
import re
from typing import Any

from memoryking.domain.lexicon import STOP

_WORD = re.compile(r"[^\W_]+")
MIN_LIST = 4  # fewer records than this are read whole, in order, with no regrouping
SHARE = 0.5  # a record is in the first group when it weighs at least this share of the best


def _tokens(text: str) -> list[str]:
    return [m.group().casefold() for m in _WORD.finditer(text)]


def _pairs(tokens: list[str]) -> list[tuple[str, str]]:
    return [
        (a, b) for a, b in zip(tokens, tokens[1:], strict=False) if a not in STOP and b not in STOP
    ]


def weigh(query: str, texts: list[str]) -> tuple[list[float], list[tuple[str, str]]]:
    """(the weight of each text, the pairs of the query that weigh anything), see the module."""
    wanted = set(_pairs(_tokens(query)))
    if not wanted or len(texts) < 2:
        return [0.0] * len(texts), []
    held = [wanted & set(_pairs(_tokens(t))) for t in texts]
    weight = {}
    for pair in wanted:
        lacking = sum(pair not in h for h in held)
        if 0 < lacking < len(texts):
            weight[pair] = math.log(len(texts) / (len(texts) - lacking))
    scores = [sum(weight.get(p, 0.0) for p in h) for h in held]
    return scores, sorted(weight, key=lambda p: -weight[p])


def phrases(query: str, pairs: list[tuple[str, str]], limit: int = 3) -> list[str]:
    """The query's own words, longest runs of the given pairs first (at most `limit`)."""
    tokens = _tokens(query)
    keep = set(pairs)
    runs: list[list[str]] = []
    for a, b in zip(tokens, tokens[1:], strict=False):
        if (a, b) in keep:
            if runs and runs[-1][-1] == a:
                runs[-1].append(b)
            else:
                runs.append([a, b])
    unique: list[str] = []
    for run in sorted(runs, key=lambda r: -len(r)):
        phrase = " ".join(run)
        if phrase not in unique:
            unique.append(phrase)
    return unique[:limit]


def split(items: list[dict[str, Any]], query: str) -> tuple[list[int], list[str]]:
    """(the places in `items` of the records that hold most of the query's pairs, the phrases
    of the query they hold). Empty when the list is shorter than MIN_LIST, when no pair tells
    the records apart, or when every record is among them."""
    if len(items) < MIN_LIST:
        return [], []
    scores, pairs = weigh(query, [str(i.get("text", "")) for i in items])
    best = max(scores, default=0.0)
    if best <= 0:
        return [], []
    first = [n for n, s in enumerate(scores) if s >= SHARE * best]
    if len(first) == len(items):
        return [], []
    top = {
        p
        for p in pairs
        if any(p in set(_pairs(_tokens(str(items[n].get("text", ""))))) for n in first)
    }
    return first, phrases(query, [p for p in pairs if p in top])


def held(text: str, shared: list[str]) -> list[str]:
    """The phrases of `shared` that `text` holds, word for word."""
    words = " ".join(_tokens(text))
    return [p for p in shared if f" {p} " in f" {words} "]


def name_first(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """`items` with the records `split` finds first, each group in the order it had."""
    first, _ = split(items, query)
    chosen = set(first)
    return [items[n] for n in first] + [i for n, i in enumerate(items) if n not in chosen]
