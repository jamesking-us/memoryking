"""Dates inside a question, resolved without a model.

"What did we decide last week", "the failure yesterday", "before the March release",
"in 2025": a question often says when. `resolve` finds explicit and relative date
expressions in a query and turns each into a UTC range, against a reference instant (the
briefing's `as_of_ms`, else its evaluated time). It also returns the query with those words
removed, so "last week" does not search for the word "week".

English only. Every instant is UTC, a day runs from 00:00:00.000 to 23:59:59.999 UTC, and a
week runs Monday to Sunday. Where an expression has more than one reading the most recent
past one is chosen and the choice is reported in `ambiguity`.

Understood (case does not matter unless said):

* ISO and slash dates: `2025-03-14`, `2025/03/14`, and the month `2025-03`.
* Month names, full or abbreviated: `March 14`, `14 March`, `March 14, 2025`, `March 2025`,
  and a bare month (`March`: its most recent occurrence not after the reference). The words
  `may`, `march` and `august` are read as months only when capitalised and either followed by a
  number or after a preposition or `the`; abbreviations only when followed by a number.
* A year after `in`, `during`, `of` or `year` (`in 2025`).
* `yesterday`, `today`, `tomorrow`, `last night` (18:00 to 06:00), `the day before yesterday`.
* `last|this|next week|month|year|quarter` as calendar periods, and `past|last N
  days|weeks|months|years` (and `past week|month|year`) as a window ending at the reference.
* `N days|weeks|months|years ago` (`a`, `an` and the numbers one to twelve as words work too):
  the day for days, and a window of a week, a month or a year centred on that instant for the
  others.
* Weekday names: `Monday`, `last Friday`, `this Tuesday`, `next Wednesday`. Plain and `last`
  mean the most recent such day before the reference's day; `this` the one in the reference's
  week; `next` the first after it.
* Quarters: `Q3`, `Q1 2025` (a bare quarter: the most recent one begun).
* `since X`, `before X`, `after X`, `until X`, `between X and Y` and `from X to Y`, for any of
  the above as X and Y.

The result is advice for ranking; it is never a claim about what the user meant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any

DAY_MS = 86_400_000
MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
ABBREVIATIONS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
NUMBERS = {
    word: number
    for number, word in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve".split()
    )
}
NUMBERS["a"] = NUMBERS["an"] = 1
AMBIGUOUS_MONTHS = {"may", "march", "august"}
LEAD_WORDS = (
    r"(?:in|since|before|after|during|until|till|from|of|last|this|next|early|late|mid|the)"
)
MONTH_NAMES = "|".join(sorted([*MONTHS, *ABBREVIATIONS], key=len, reverse=True))
MONTH = rf"(?P<month>{MONTH_NAMES})\.?"
NUMBER_WORDS = "|".join(NUMBERS)
UNITS = "day|week|month|year"

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "iso_date",
        re.compile(
            r"(?:\bon\s+)?(?<![\d/-])(?P<y>\d{4})[-/](?P<m>\d{1,2})[-/](?P<d>\d{1,2})(?!\d)"
        ),
    ),
    ("iso_month", re.compile(r"(?<![\d/-])(?P<y>\d{4})-(?P<m>\d{2})(?![\d-])")),
    (
        "month_day",
        re.compile(
            rf"\b(?:on\s+)?{MONTH}\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?(?!\d)(?:,?\s+(?P<y>\d{{4}})\b)?",
            re.I,
        ),
    ),
    (
        "day_month",
        re.compile(
            rf"\b(?:on\s+)?(?:the\s+)?(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{MONTH}(?:,?\s+(?P<y>\d{{4}})\b)?",
            re.I,
        ),
    ),
    ("month_year", re.compile(rf"\b{MONTH},?\s+(?P<y>\d{{4}})\b", re.I)),
    ("month", re.compile(rf"\b{MONTH}\b", re.I)),
    ("quarter", re.compile(r"\bQ(?P<q>[1-4])(?:\s+(?P<y>\d{4}))?\b", re.I)),
    ("year", re.compile(r"\b(?P<y>(?:19|20)\d{2})\b")),
    ("day_before_yesterday", re.compile(r"\b(?:the\s+)?day\s+before\s+yesterday\b", re.I)),
    ("last_night", re.compile(r"\blast\s+night\b", re.I)),
    ("day_word", re.compile(r"\b(?P<w>yesterday|today|tomorrow)\b", re.I)),
    (
        "rolling",
        re.compile(
            rf"\b(?:in\s+the\s+)?(?:last|past)\s+(?P<n>\d+|{NUMBER_WORDS})\s+(?P<u>{UNITS})s?\b",
            re.I,
        ),
    ),
    ("rolling", re.compile(rf"\b(?:in\s+the\s+)?past\s+(?P<u>{UNITS})\b", re.I)),
    ("period", re.compile(r"\b(?P<rel>last|this|next)\s+(?P<u>week|month|year|quarter)\b", re.I)),
    (
        "ago",
        re.compile(rf"\b(?P<n>\d+|{NUMBER_WORDS})\s+(?P<u>{UNITS})s?\s+ago\b", re.I),
    ),
    (
        "weekday",
        re.compile(
            r"\b(?:on\s+)?(?:(?P<rel>last|this|next)\s+)?(?P<w>" + "|".join(WEEKDAYS) + r")\b",
            re.I,
        ),
    ),
]
OPERATOR = re.compile(r"\b(?P<op>since|before|after|until|till|prior\s+to)\s+(?:the\s+)?$", re.I)
BETWEEN = re.compile(r"\bbetween\s+(?:the\s+)?$", re.I)
FROM = re.compile(r"\bfrom\s+(?:the\s+)?$", re.I)
JOIN_AND = re.compile(r"\s+and\s+(?:the\s+)?", re.I)
JOIN_TO = re.compile(r"\s+(?:to|through|until|till)\s+(?:the\s+)?", re.I)
YEAR_LEAD = re.compile(r"\b(?:in|during|of|year)\s+$", re.I)


@dataclass(frozen=True)
class Expression:
    """One piece of the query that names a time, and the UTC range it resolves to."""

    text: str
    kind: str
    from_ms: int
    to_ms: int
    start: int  # character offsets of `text` in the query
    end: int
    note: str | None = None

    def trace(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "kind": self.kind,
            "from_ms": self.from_ms,
            "to_ms": self.to_ms,
            "from": _iso(self.from_ms),
            "to": _iso(self.to_ms),
            **({"note": self.note} if self.note else {}),
        }


@dataclass
class Understanding:
    reference_ms: int
    expressions: list[Expression] = field(default_factory=list)
    remaining: str = ""
    ambiguity: list[str] = field(default_factory=list)

    @property
    def ranges(self) -> list[tuple[int, int]]:
        return [(e.from_ms, e.to_ms) for e in self.expressions]

    def trace(self) -> dict[str, Any]:
        return {
            "reference_ms": self.reference_ms,
            "expressions": [e.trace() for e in self.expressions],
            "ranges": [[a, b] for a, b in self.ranges],
            "removed_from_terms": [e.text for e in self.expressions],
            "ambiguity": self.ambiguity,
        }


def _iso(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    )


def _ms(day: date, hour: int = 0) -> int:
    start = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
    return int(start.timestamp() * 1000)


def _day(day: date) -> tuple[int, int]:
    return _ms(day), _ms(day + timedelta(days=1)) - 1


def _month(year: int, month: int) -> tuple[int, int]:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return _ms(date(year, month, 1)), _ms(nxt) - 1


def _year(year: int) -> tuple[int, int]:
    return _ms(date(year, 1, 1)), _ms(date(year + 1, 1, 1)) - 1


def _quarter(year: int, quarter: int) -> tuple[int, int]:
    first = 3 * (quarter - 1) + 1
    return _month(year, first)[0], _month(year, first + 2)[1]


def _week(day: date) -> tuple[int, int]:
    monday = day - timedelta(days=day.weekday())
    return _ms(monday), _ms(monday + timedelta(days=7)) - 1


def _shift_months(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _number(text: str | None) -> int:
    if text is None:
        return 1
    return int(text) if text.isdigit() else NUMBERS[text.lower()]


class _Resolver:
    def __init__(self, query: str, now_ms: int):
        self.query = query
        self.now = now_ms
        self.today = datetime.fromtimestamp(now_ms / 1000, timezone.utc).date()
        self.notes: list[str] = []
        self.pending: list[str] = []

    def note(self, text: str) -> None:
        """Remember an assumption of the atom being resolved; kept only if the atom is."""
        self.pending.append(text)

    # --- one atom -----------------------------------------------------------------------
    def atom(self, kind: str, m: re.Match[str]) -> tuple[str, tuple[int, int], str | None] | None:
        """(kind, range, note) for a match, or None when it is not really a date."""
        g = m.groupdict()
        today = self.today
        try:
            if kind == "iso_date":
                return kind, _day(date(int(g["y"]), int(g["m"]), int(g["d"]))), None
            if kind == "iso_month":
                if not 1 <= int(g["m"]) <= 12:
                    return None
                return kind, _month(int(g["y"]), int(g["m"])), None
            if kind in {"month_day", "day_month", "month_year", "month"}:
                return self.month(kind, m, g)
            if kind == "quarter":
                quarter = int(g["q"])
                if g["y"]:
                    return kind, _quarter(int(g["y"]), quarter), None
                year = today.year
                if _quarter(year, quarter)[0] > self.now:
                    year -= 1
                self.note("quarter_without_year_assumes_most_recent_past")
                return kind, _quarter(year, quarter), "most recent past quarter"
            if kind == "year":
                return kind, _year(int(g["y"])), None
            if kind == "day_before_yesterday":
                return kind, _day(today - timedelta(days=2)), None
            if kind == "last_night":
                return (
                    kind,
                    (_ms(today - timedelta(days=1), 18), _ms(today, 6) - 1),
                    "18:00 yesterday to 06:00 today UTC",
                )
            if kind == "day_word":
                offset = {"yesterday": -1, "today": 0, "tomorrow": 1}[g["w"].lower()]
                return kind, _day(today + timedelta(days=offset)), None
            if kind == "rolling":
                n, unit = _number(g.get("n")), g["u"].lower()
                return kind, (self.now - self.span(n, unit), self.now), None
            if kind == "period":
                return self.period(g["rel"].lower(), g["u"].lower())
            if kind == "ago":
                return self.ago(_number(g["n"]), g["u"].lower())
            if kind == "weekday":
                return self.weekday(g["rel"], g["w"].lower())
        except ValueError:
            return None  # 31 February and the like
        return None

    def span(self, n: int, unit: str) -> int:
        """A window of n units back from the reference, in milliseconds."""
        if unit == "day":
            return n * DAY_MS
        if unit == "week":
            return n * 7 * DAY_MS
        year, month = _shift_months(
            self.today.year, self.today.month, -(n * (12 if unit == "year" else 1))
        )
        start = _ms(date(year, month, min(self.today.day, 28)))
        return self.now - start

    def month(
        self, kind: str, m: re.Match[str], g: dict[str, Any]
    ) -> tuple[str, tuple[int, int], str | None] | None:
        name = g["month"].lower()
        abbreviated = name in ABBREVIATIONS and name not in MONTHS
        number = MONTHS.get(name) or ABBREVIATIONS[name]
        has_number = kind != "month"
        if abbreviated and not has_number:
            return None
        if name in AMBIGUOUS_MONTHS and not has_number:
            word = m.string[m.start("month") : m.end("month")]
            before = m.string[: m.start("month")]
            if not word[0].isupper() or not re.search(rf"\b{LEAD_WORDS}\s+$", before, re.I):
                return None
        if kind == "month_year":
            return kind, _month(int(g["y"]), number), None
        if kind == "month":
            year = (
                self.today.year
                if _month(self.today.year, number)[0] <= self.now
                else self.today.year - 1
            )
            self.note("month_without_year_assumes_most_recent_past")
            return kind, _month(year, number), "most recent past occurrence"
        day = int(g["d"])
        if g.get("y"):
            return kind, _day(date(int(g["y"]), number, day)), None
        year = self.today.year
        if date(year, number, day) > self.today:
            year -= 1
        self.note("date_without_year_assumes_most_recent_past")
        return kind, _day(date(year, number, day)), "most recent past occurrence"

    def period(self, rel: str, unit: str) -> tuple[str, tuple[int, int], str | None]:
        today = self.today
        step = {"last": -1, "this": 0, "next": 1}[rel]
        if unit == "week":
            if rel == "last":
                self.note("last_week_is_the_previous_calendar_week")
            return "period", _week(today + timedelta(days=7 * step)), "Monday to Sunday UTC"
        if unit == "month":
            year, month = _shift_months(today.year, today.month, step)
            return "period", _month(year, month), None
        if unit == "year":
            return "period", _year(today.year + step), None
        quarter = (today.month - 1) // 3 + step
        year = today.year + quarter // 4
        return "period", _quarter(year, quarter % 4 + 1), None

    def ago(self, n: int, unit: str) -> tuple[str, tuple[int, int], str | None]:
        today = self.today
        if unit == "day":
            return "ago", _day(today - timedelta(days=n)), None
        if unit == "week":
            centre = self.now - n * 7 * DAY_MS
            return (
                "ago",
                (centre - 3 * DAY_MS - DAY_MS // 2, centre + 3 * DAY_MS + DAY_MS // 2),
                "a week around that point",
            )
        if unit == "month":
            centre = self.now - self.span(n, "month")
            return "ago", (centre - 15 * DAY_MS, centre + 15 * DAY_MS), "a month around that point"
        centre = self.now - self.span(n, "year")
        return "ago", (centre - 183 * DAY_MS, centre + 183 * DAY_MS), "a year around that point"

    def weekday(self, rel: str | None, name: str) -> tuple[str, tuple[int, int], str | None]:
        today = self.today
        target = WEEKDAYS[name]
        rel = rel.lower() if rel else None
        if rel == "this":
            return (
                "weekday",
                _day(today - timedelta(days=today.weekday()) + timedelta(days=target)),
                None,
            )
        if rel == "next":
            ahead = (target - today.weekday() - 1) % 7 + 1
            return "weekday", _day(today + timedelta(days=ahead)), None
        back = (today.weekday() - target - 1) % 7 + 1  # strictly before today
        if rel is None:
            self.note("weekday_without_qualifier_assumes_most_recent_past")
        return "weekday", _day(today - timedelta(days=back)), None

    # --- the whole query ----------------------------------------------------------------
    def atoms(self) -> list[tuple[int, int, str, tuple[int, int], str | None, tuple[str, ...]]]:
        found: list[tuple[int, int, int, str, tuple[int, int], str | None, tuple[str, ...]]] = []
        for order, (kind, pattern) in enumerate(PATTERNS):
            for m in pattern.finditer(self.query):
                self.pending = []
                result = self.atom(kind, m)
                if result is None:
                    continue
                found.append(
                    (
                        m.start(),
                        m.end(),
                        order,
                        result[0],
                        result[1],
                        result[2],
                        tuple(self.pending),
                    )
                )
        # Longest first, then earliest, then pattern order; later overlapping matches lose.
        found.sort(key=lambda item: (-(item[1] - item[0]), item[0], item[2]))
        taken: list[tuple[int, int, str, tuple[int, int], str | None, tuple[str, ...]]] = []
        for start, end, _, kind, rng, note, tags in found:
            if all(end <= s or start >= e for s, e, *_ in taken):
                taken.append((start, end, kind, rng, note, tags))
        taken.sort()
        return taken

    def run(self) -> Understanding:
        text = self.query
        atoms = self.atoms()
        expressions: list[Expression] = []

        def keep(expression: Expression, tags: tuple[str, ...]) -> None:
            tail = re.match(r"['\u2019]s\b", text[expression.end :])  # "yesterday's failure"
            if tail:
                expression = replace(expression, end=expression.end + len(tail.group()))
                expression = replace(expression, text=text[expression.start : expression.end])
            expressions.append(expression)
            self.notes.extend(t for t in tags if t not in self.notes)

        index = 0
        while index < len(atoms):
            start, end, kind, rng, note, tags = atoms[index]
            before = text[:start]
            if kind == "year" and not YEAR_LEAD.search(before) and not self.joined(atoms, index):
                index += 1  # a bare four-digit number is not a year
                continue
            lead = YEAR_LEAD.search(before) if kind == "year" else None
            if lead:
                start = lead.start()
            between = BETWEEN.search(before)
            from_ = FROM.search(before)
            nxt = atoms[index + 1] if index + 1 < len(atoms) else None
            if nxt is not None and (between or from_):
                joiner = (JOIN_AND if between else JOIN_TO).fullmatch(text[end : nxt[0]])
                lead_match = between or from_
                if joiner and lead_match:
                    low, high = rng[0], nxt[3][1]
                    if low > high:
                        # "between January and March 2025": the open month is the earlier year.
                        low = self.a_year_earlier(low, high)
                    keep(
                        Expression(
                            text[lead_match.start() : nxt[1]],
                            "between",
                            low,
                            high,
                            lead_match.start(),
                            nxt[1],
                        ),
                        (*tags, *nxt[5]),
                    )
                    index += 2
                    continue
            operator = OPERATOR.search(before)
            if operator:
                word = re.sub(r"\s+", " ", operator.group("op").lower())
                low, high = {
                    "since": (rng[0], max(self.now, rng[1])),
                    "after": (rng[1] + 1, max(self.now, rng[1] + 1)),
                    "before": (0, rng[0] - 1),
                    "prior to": (0, rng[0] - 1),
                    "until": (0, rng[1]),
                    "till": (0, rng[1]),
                }[word]
                keep(
                    Expression(
                        text[operator.start() : end],
                        word.replace(" ", "_"),
                        low,
                        high,
                        operator.start(),
                        end,
                        note,
                    ),
                    tags,
                )
            elif kind == "year" and not lead:
                index += 1
                continue
            else:
                keep(Expression(text[start:end], kind, rng[0], rng[1], start, end, note), tags)
            index += 1
        remaining = text
        for e in sorted(expressions, key=lambda item: -item.start):
            remaining = remaining[: e.start] + " " + remaining[e.end :]
        return Understanding(
            self.now,
            sorted(expressions, key=lambda item: item.start),
            re.sub(r"\s+", " ", remaining).strip(),
            self.notes,
        )

    @staticmethod
    def a_year_earlier(low: int, high: int) -> int:
        """`low` moved back whole years until it is not after `high`; unchanged if impossible."""
        moment = datetime.fromtimestamp(low / 1000, timezone.utc)
        for _ in range(200):
            try:
                moment = moment.replace(year=moment.year - 1)
            except ValueError:
                return low
            if int(moment.timestamp() * 1000) <= high:
                return int(moment.timestamp() * 1000)
        return low

    def joined(self, atoms: list[Any], index: int) -> bool:
        """Whether a bare year belongs to an operator or a between/from pair."""
        start = atoms[index][0]
        before = self.query[:start]
        if OPERATOR.search(before) or BETWEEN.search(before) or FROM.search(before):
            return True
        if index > 0:
            previous = atoms[index - 1]
            gap = self.query[previous[1] : start]
            lead = self.query[: previous[0]]
            return bool(
                (BETWEEN.search(lead) and JOIN_AND.fullmatch(gap))
                or (FROM.search(lead) and JOIN_TO.fullmatch(gap))
            )
        return False


def resolve(query: str, reference_ms: int) -> Understanding:
    """Date expressions in `query`, as UTC ranges against `reference_ms` (Unix ms)."""
    return _Resolver(query, reference_ms).run()


def in_ranges(ranges: list[tuple[int, int]], start: int | None, end: int | None) -> bool:
    """Whether [start, end] (either end open as None) overlaps any range."""
    low = -1 if start is None else start
    high = 1 << 62 if end is None else end
    return any(low <= b and a <= high for a, b in ranges)
