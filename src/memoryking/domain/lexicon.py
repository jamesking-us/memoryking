"""Words, word forms and BM25-style relevance; deterministic, dependency-free, no storage.

One analyzer serves the index, the query and the pure scorer, so all three agree on what a
word is. Text becomes lowercase tokens (runs of letters and digits; an underscore ends a
token). Ordinary English words also get a stem, so "retry", "retries", "retried" and
"retrying" meet; code-shaped words (anything with a digit, or joined by underscores) and
non-ASCII words are never stemmed and match exactly.

The index text fed to both full-text engines is the tokens in their original order
followed by one marked stem token per stemmable word. Nothing in it depends on how an
engine tokenizes paths, tickets or symbols, so SQLite FTS5 and the PostgreSQL `simple`
configuration see the same words. Statistics (how many documents hold a term) are counted
over exactly those index tokens, which makes purge able to undo a capture from the stored
index text alone.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache

INDEX_CONFIGURATION = "lexical-v2"  # the index text and statistics format
STEM_MARK = "xqz9"  # prefix of a stem token in index text; a real word does not start so
MAX_DOC_TERMS = 4096  # distinct index tokens one document adds to the statistics
QUERY_TERM_LIMIT = 32

# Function words only. Words that carry meaning in code and incident notes stay
# searchable: not, no, null, none, error, test, fail, true, false, all, any, new, old.
STOP = frozenset(
    """a about above after again against am an and are as at be because been before being
    below between both but by can could did do does doing down during each few for from further
    had has have having he her here hers herself him himself his how i if in into is it its
    itself just me more most my myself nor of off on once only or other ought our ours ourselves
    out over own same she should so some such than that the their theirs them themselves then
    there these they this those through to too under until up very was we were what when where
    which while who whom why will with would you your yours yourself yourselves also may might
    must shall""".split()
)

_CHUNK = re.compile(r"\w+")
_DIGIT = re.compile(r"\d")


# --- A Porter-style suffix stemmer (M. F. Porter, 1980), ASCII letters only --------------


def _consonant(word: str, i: int) -> bool:
    c = word[i]
    if c in "aeiou":
        return False
    if c == "y":
        return i == 0 or not _consonant(word, i - 1)
    return True


def _measure(stem: str) -> int:
    count, i, size = 0, 0, len(stem)
    while i < size and _consonant(stem, i):
        i += 1
    while i < size:
        while i < size and not _consonant(stem, i):
            i += 1
        if i >= size:
            break
        count += 1
        while i < size and _consonant(stem, i):
            i += 1
    return count


def _has_vowel(stem: str) -> bool:
    return any(not _consonant(stem, i) for i in range(len(stem)))


def _double(word: str) -> bool:
    return len(word) >= 2 and word[-1] == word[-2] and _consonant(word, len(word) - 1)


def _cvc(word: str) -> bool:
    n = len(word)
    return (
        n >= 3
        and _consonant(word, n - 3)
        and not _consonant(word, n - 2)
        and _consonant(word, n - 1)
        and word[-1] not in "wxy"
    )


_STEP2 = (
    ("ational", "ate"),
    ("tional", "tion"),
    ("enci", "ence"),
    ("anci", "ance"),
    ("izer", "ize"),
    ("abli", "able"),
    ("alli", "al"),
    ("entli", "ent"),
    ("eli", "e"),
    ("ousli", "ous"),
    ("ization", "ize"),
    ("ation", "ate"),
    ("ator", "ate"),
    ("alism", "al"),
    ("iveness", "ive"),
    ("fulness", "ful"),
    ("ousness", "ous"),
    ("aliti", "al"),
    ("iviti", "ive"),
    ("biliti", "ble"),
)
_STEP3 = (
    ("icate", "ic"),
    ("ative", ""),
    ("alize", "al"),
    ("iciti", "ic"),
    ("ical", "ic"),
    ("ful", ""),
    ("ness", ""),
)
_STEP4 = (
    "al ance ence er ic able ible ant ement ment ent ou ism ate iti ous ive ize".split()
)


def _replace(word: str, rules: tuple[tuple[str, str], ...]) -> str:
    for suffix, replacement in rules:
        if word.endswith(suffix):
            stem = word[: -len(suffix)]
            return stem + replacement if _measure(stem) > 0 else word
    return word


@lru_cache(maxsize=65536)
def porter(word: str) -> str:
    if len(word) <= 2:
        return word
    # Step 1a: plurals.
    if word.endswith("sses"):
        word = word[:-2]
    elif word.endswith("ies"):
        word = word[:-2]
    elif word.endswith("ss"):
        pass
    elif word.endswith("s"):
        word = word[:-1]
    # Step 1b: -eed, -ed, -ing.
    if word.endswith("eed"):
        if _measure(word[:-3]) > 0:
            word = word[:-1]
    else:
        for suffix in ("ed", "ing"):
            if word.endswith(suffix) and _has_vowel(word[: -len(suffix)]):
                word = word[: -len(suffix)]
                if word.endswith(("at", "bl", "iz")):
                    word += "e"
                elif _double(word) and word[-1] not in "lsz":
                    word = word[:-1]
                elif _measure(word) == 1 and _cvc(word):
                    word += "e"
                break
    # Step 1c: terminal y.
    if word.endswith("y") and _has_vowel(word[:-1]):
        word = word[:-1] + "i"
    word = _replace(word, _STEP2)
    word = _replace(word, _STEP3)
    # Step 4: strip a suffix from long stems.
    for suffix in _STEP4:
        if word.endswith(suffix):
            if _measure(word[: -len(suffix)]) > 1:
                word = word[: -len(suffix)]
            break
    else:
        if word.endswith("ion") and word[:-3].endswith(("s", "t")) and _measure(word[:-3]) > 1:
            word = word[:-3]
    # Step 5: a final e, then a doubled l.
    if word.endswith("e"):
        measure = _measure(word[:-1])
        if measure > 1 or (measure == 1 and not _cvc(word[:-1])):
            word = word[:-1]
    if _measure(word) > 1 and _double(word) and word.endswith("l"):
        word = word[:-1]
    return word


def stem(token: str) -> str | None:
    """The stem of an ordinary English word; None for anything that must match exactly."""
    if len(token) < 3 or not token.isascii() or not token.isalpha() or token in STOP:
        return None
    return porter(token)


# --- Analysis -----------------------------------------------------------------------------


def analyze(text: str) -> list[tuple[str, str | None]]:
    """(token, stem) in text order. The stem is None for words that are matched exactly:
    stop words, short or non-ASCII words, and any word with a digit or joined by an
    underscore (identifiers such as retry_charge or PAY-142 keep their exact tokens)."""
    result: list[tuple[str, str | None]] = []
    for chunk in _CHUNK.findall(text.casefold()):
        exact = "_" in chunk or _DIGIT.search(chunk) is not None
        for part in chunk.split("_"):
            if part:
                result.append((part, None if exact else stem(part)))
    return result


def index_text(text: str) -> str:
    """What both full-text engines index: every token in order, then the marked stems."""
    pairs = analyze(text)
    stems = dict.fromkeys(STEM_MARK + s for _, s in pairs if s is not None)
    return " ".join([t for t, _ in pairs] + list(stems))


def index_terms(indexed: str) -> tuple[list[str], int]:
    """The statistics view of index text: its distinct non-stop tokens (bounded) and its
    length in non-stop tokens. Defined on the stored index text so purge can reverse it."""
    seen: dict[str, None] = {}
    length = 0
    for token in indexed.split():
        if token.startswith(STEM_MARK):
            seen.setdefault(token, None)
        elif token not in STOP:
            length += 1
            seen.setdefault(token, None)
    return list(seen)[:MAX_DOC_TERMS], length


def plain_text(indexed: str) -> str:
    """Index text without its marked stems (the words of the document, in order)."""
    return " ".join(t for t in indexed.split() if not t.startswith(STEM_MARK))


@dataclass(frozen=True)
class Term:
    """One query concept: its index key and the exact words that were asked for."""

    key: str  # the marked stem, or the exact token when it has no stem
    words: tuple[str, ...]  # exact tokens of the query that map to this key
    stemmed: bool

    def alternatives(self) -> list[str]:
        """What to look for in an index: marked stem plus the exact words, so rows indexed
        before stems existed are still found by their exact token."""
        return list(dict.fromkeys([self.key, *self.words]))


def query_window(text: str) -> tuple[list[str], bool]:
    """The first QUERY_TERM_LIMIT distinct non-stop tokens, sorted, plus a truncation flag."""
    seen: dict[str, None] = {}
    for token, _ in analyze(text):
        if token not in STOP:
            seen.setdefault(token, None)
    return sorted(list(seen)[:QUERY_TERM_LIMIT]), len(seen) > QUERY_TERM_LIMIT


def query_terms(text: str) -> list[Term]:
    """Query concepts, one per distinct index key, ordered by key."""
    window = set(query_window(text)[0])
    grouped: dict[str, list[str]] = {}
    marks: dict[str, bool] = {}
    for token, stemmed in analyze(text):
        if token in window:
            key = STEM_MARK + stemmed if stemmed is not None else token
            words = grouped.setdefault(key, [])
            if token not in words:
                words.append(token)
            marks[key] = stemmed is not None
    return [Term(key, tuple(sorted(words)), marks[key]) for key, words in sorted(grouped.items())]


# --- BM25-style relevance ---------------------------------------------------------------

K1 = 1.2  # term-frequency saturation
B = 0.75  # length normalisation strength
STEM_WEIGHT = 0.8  # a stem-only match counts this much of an exact one


@dataclass(frozen=True)
class Corpus:
    """What the whole project looks like: documents, total length, and term document counts.

    Built from stored statistics; an empty corpus scores every term equally and treats the
    document as average length, which is the neutral reading."""

    documents: int = 0
    tokens: int = 0
    frequencies: Mapping[str, int] | None = None

    def frequency(self, key: str) -> int:
        return int((self.frequencies or {}).get(key, 0))


def idf(corpus: Corpus, key: str) -> float:
    if corpus.documents <= 0:
        return 1.0
    df = min(max(corpus.frequency(key), 0), corpus.documents)
    return math.log(1.0 + (corpus.documents - df + 0.5) / (df + 0.5))


def score(terms: list[Term], text: str, corpus: Corpus) -> int:
    """Relevance in millionths, 0 when no term occurs.

    Each term contributes idf * tf*(K1+1) / (tf + K1*(1-B+B*length/average)), with tf
    counting exact words fully and other word forms at STEM_WEIGHT. The sum is divided by
    the largest sum any document could reach (every term saturated, idf*(K1+1)), so the
    result lies in 0..1,000,000 without clamping and keeps ordering at the top; a document
    holding every term once at average length scores about 1,000,000/(K1+1)."""
    if not terms:
        return 0
    pairs = [(t, s) for t, s in analyze(text) if t not in STOP]
    if not pairs:
        return 0
    plain = Counter(t for t, _ in pairs)
    keys = Counter(STEM_MARK + s if s is not None else t for t, s in pairs)
    length = len(pairs)
    average = corpus.tokens / corpus.documents if corpus.documents and corpus.tokens else length
    norm = 1.0 - B + B * length / average
    total = ceiling = 0.0
    for term in terms:
        weight = idf(corpus, term.key)
        ceiling += weight * (K1 + 1.0)
        exact = sum(plain[w] for w in term.words)
        frequency = exact + STEM_WEIGHT * max(0, keys[term.key] - exact) if term.stemmed else exact
        if frequency > 0:
            total += weight * frequency * (K1 + 1.0) / (frequency + K1 * norm)
    if total <= 0 or ceiling <= 0:
        return 0
    return max(1, min(1_000_000, int(1_000_000 * total / ceiling)))
