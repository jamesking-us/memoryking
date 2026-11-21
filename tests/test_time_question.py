"""Dates inside a question: the resolver against a fixed instant, and briefings on both backends."""

import json
import unittest
from datetime import date

import test_knowledge as knowledge
import test_postgres_parity as pg
from test_lexical_search import LexicalCases

from memoryking.domain.local import LocalError
from memoryking.domain.timeexpr import _ms, in_ranges, resolve

NOW = _ms(date(2026, 11, 18), 15)  # Wednesday 2026-11-18 15:00 UTC


def span(query, now=NOW):
    """(kind, from, to) of each expression, as dates (times when not whole days)."""
    understood = resolve(query, now)
    return [(e.kind, e.trace()["from"], e.trace()["to"]) for e in understood.expressions]


def day(text):
    return (text + "T00:00:00.000Z", text + "T23:59:59.999Z")


class ResolverTests(unittest.TestCase):
    def one(self, query, kind, start, end, remaining=None):
        found = span(query)
        self.assertEqual(1, len(found), (query, found))
        self.assertEqual((kind, start, end), found[0], query)
        if remaining is not None:
            self.assertEqual(remaining, resolve(query, NOW).remaining, query)

    def test_iso_and_slash_dates_and_months(self):
        self.one("deploy on 2025-03-14 failed", "iso_date", *day("2025-03-14"), "deploy failed")
        self.one("2025/3/4", "iso_date", *day("2025-03-04"))
        self.one(
            "changes in 2025-03",
            "iso_month",
            "2025-03-01T00:00:00.000Z",
            "2025-03-31T23:59:59.999Z",
        )
        self.assertEqual([], span("2025-02-31 is not a date"))

    def test_month_names_with_and_without_a_year(self):
        self.one("March 14, 2025 outage", "month_day", *day("2025-03-14"), "outage")
        self.one("the 5th of June 2024 incident", "day_month", *day("2024-06-05"), "incident")
        self.one("March 2025", "month_year", "2025-03-01T00:00:00.000Z", "2025-03-31T23:59:59.999Z")
        # Without a year: the most recent past occurrence, reported.
        self.one("on May 5 we shipped", "month_day", *day("2026-05-05"), "we shipped")
        self.one("Dec 3 release", "month_day", *day("2025-12-03"), "release")
        understood = resolve("Dec 3 release", NOW)
        self.assertIn("date_without_year_assumes_most_recent_past", understood.ambiguity)
        self.one("in November", "month", "2026-11-01T00:00:00.000Z", "2026-11-30T23:59:59.999Z")
        self.one("in December", "month", "2025-12-01T00:00:00.000Z", "2025-12-31T23:59:59.999Z")

    def test_words_that_are_also_months_are_not_read_as_dates_in_prose(self):
        self.assertEqual([], span("what may happen if we march on"))
        self.assertEqual([], span("an august gathering"))
        self.assertEqual([], span("the sat nav and the mar plan"))
        self.assertEqual(1, len(span("before the March release")))

    def test_a_bare_number_is_not_a_year_but_in_year_is(self):
        self.assertEqual([], span("port 2024 is open"))
        self.one(
            "what happened in 2025",
            "year",
            "2025-01-01T00:00:00.000Z",
            "2025-12-31T23:59:59.999Z",
            "what happened",
        )

    def test_yesterday_today_tomorrow_and_last_night(self):
        self.one("yesterday's failure", "day_word", *day("2026-11-17"), "failure")
        self.one("today", "day_word", *day("2026-11-18"))
        self.one("tomorrow", "day_word", *day("2026-11-19"))
        self.one("the day before yesterday", "day_before_yesterday", *day("2026-11-16"))
        self.one(
            "last night outage",
            "last_night",
            "2026-11-17T18:00:00.000Z",
            "2026-11-18T05:59:59.999Z",
            "outage",
        )

    def test_last_this_next_calendar_periods(self):
        monday_to_sunday = ("2026-11-09T00:00:00.000Z", "2026-11-15T23:59:59.999Z")
        self.one("what did we decide last week", "period", *monday_to_sunday, "what did we decide")
        self.assertIn(
            "last_week_is_the_previous_calendar_week", resolve("last week", NOW).ambiguity
        )
        self.one("this week", "period", "2026-11-16T00:00:00.000Z", "2026-11-22T23:59:59.999Z")
        self.one("next week", "period", "2026-11-23T00:00:00.000Z", "2026-11-29T23:59:59.999Z")
        self.one("last month", "period", "2026-10-01T00:00:00.000Z", "2026-10-31T23:59:59.999Z")
        self.one("this year", "period", "2026-01-01T00:00:00.000Z", "2026-12-31T23:59:59.999Z")
        self.one("last quarter", "period", "2026-07-01T00:00:00.000Z", "2026-09-30T23:59:59.999Z")
        self.one("this quarter", "period", "2026-10-01T00:00:00.000Z", "2026-12-31T23:59:59.999Z")

    def test_a_year_boundary_for_last_month_and_quarter(self):
        january = _ms(date(2027, 1, 20), 9)
        self.assertEqual(
            [("period", "2026-12-01T00:00:00.000Z", "2026-12-31T23:59:59.999Z")],
            span("last month", january),
        )
        self.assertEqual(
            [("period", "2026-10-01T00:00:00.000Z", "2026-12-31T23:59:59.999Z")],
            span("last quarter", january),
        )

    def test_rolling_windows_and_n_units_ago(self):
        found = span("in the past 30 days")
        self.assertEqual(
            [("rolling", "2026-10-19T15:00:00.000Z", "2026-11-18T15:00:00.000Z")], found
        )
        self.assertEqual("rolling", span("last 2 weeks")[0][0])
        self.one("three days ago", "days_ago" if False else "ago", *day("2026-11-15"))
        self.one("a day ago", "ago", *day("2026-11-17"))
        weeks = span("2 weeks ago the deploy failed")
        self.assertEqual("ago", weeks[0][0])
        # A week around the point two weeks back (2026-11-04 15:00).
        self.assertEqual(("2026-11-01T03:00:00.000Z", "2026-11-08T03:00:00.000Z"), weeks[0][1:])
        self.assertEqual("ago", span("6 months ago")[0][0])

    def test_weekday_names(self):
        self.one("on Friday we shipped", "weekday", *day("2026-11-13"), "we shipped")
        self.assertIn(
            "weekday_without_qualifier_assumes_most_recent_past",
            resolve("on Friday we shipped", NOW).ambiguity,
        )
        self.one("last Monday's meeting", "weekday", *day("2026-11-16"), "meeting")
        self.one("Wednesday", "weekday", *day("2026-11-11"))  # strictly before today
        self.one("this Friday", "weekday", *day("2026-11-20"))
        self.one("next Monday", "weekday", *day("2026-11-23"))

    def test_quarters(self):
        self.one(
            "Q1 2025 numbers",
            "quarter",
            "2025-01-01T00:00:00.000Z",
            "2025-03-31T23:59:59.999Z",
            "numbers",
        )
        self.one("Q3 numbers", "quarter", "2026-07-01T00:00:00.000Z", "2026-09-30T23:59:59.999Z")
        self.one("Q4 numbers", "quarter", "2026-10-01T00:00:00.000Z", "2026-12-31T23:59:59.999Z")

    def test_since_before_after_until_between_and_from_to(self):
        self.one(
            "since March 3", "since", "2026-03-03T00:00:00.000Z", "2026-11-18T15:00:00.000Z", ""
        )
        self.one(
            "since last month", "since", "2026-10-01T00:00:00.000Z", "2026-11-18T15:00:00.000Z"
        )
        self.one(
            "before the March release",
            "before",
            "1970-01-01T00:00:00.000Z",
            "2026-02-28T23:59:59.999Z",
            "release",
        )
        self.one(
            "after 2025-06-01 changes",
            "after",
            "2025-06-02T00:00:00.000Z",
            "2026-11-18T15:00:00.000Z",
            "changes",
        )
        self.one("until June 2025", "until", "1970-01-01T00:00:00.000Z", "2025-06-30T23:59:59.999Z")
        self.one(
            "between January and March 2025",
            "between",
            "2025-01-01T00:00:00.000Z",
            "2025-03-31T23:59:59.999Z",
            "",
        )
        self.one(
            "between 2025-01-01 and 2025-02-15",
            "between",
            "2025-01-01T00:00:00.000Z",
            "2025-02-15T23:59:59.999Z",
        )
        self.one(
            "from 2025-01-01 to 2025-02-15 changes",
            "between",
            "2025-01-01T00:00:00.000Z",
            "2025-02-15T23:59:59.999Z",
            "changes",
        )
        self.one(
            "between 2020 and 2022",
            "between",
            "2020-01-01T00:00:00.000Z",
            "2022-12-31T23:59:59.999Z",
        )

    def test_several_expressions_give_several_ranges(self):
        understood = resolve("compare yesterday with 2025-01-05 results", NOW)
        self.assertEqual(2, len(understood.ranges))
        self.assertEqual("compare with results", understood.remaining)
        self.assertTrue(
            in_ranges(understood.ranges, _ms(date(2025, 1, 5), 3), _ms(date(2025, 1, 5), 3))
        )
        self.assertFalse(in_ranges(understood.ranges, _ms(date(2025, 1, 6)), _ms(date(2025, 1, 6))))

    def test_no_time_in_a_query_changes_nothing(self):
        understood = resolve("retry backoff exceeded the deadline", NOW)
        self.assertEqual([], understood.expressions)
        self.assertEqual("retry backoff exceeded the deadline", understood.remaining)

    def test_the_trace_is_json_and_deterministic(self):
        first = resolve("last week and Q1 2025", NOW).trace()
        self.assertEqual(first, resolve("last week and Q1 2025", NOW).trace())
        json.dumps(first)
        self.assertEqual(["last week", "Q1 2025"], first["removed_from_terms"])

    def test_open_ended_validity_overlaps_everything(self):
        self.assertTrue(in_ranges([(10, 20)], None, None))
        self.assertTrue(in_ranges([(10, 20)], 15, None))
        self.assertFalse(in_ranges([(10, 20)], 21, None))
        self.assertFalse(in_ranges([(10, 20)], None, 9))


DAY = 86_400_000
HOUR = 3_600_000


def stamp(ms):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


class TimeBriefingCases(LexicalCases):
    """Briefings whose questions carry time, on SQLite and PostgreSQL.

    Dates are placed relative to the store's own clock, because that is the instant a
    briefing without `as_of_ms` resolves "last week" against."""

    def setup_corpus(self):
        self.now = self.store.clock.utc_ms()
        self.last_week = resolve("last week", self.now).ranges[0]
        text = "the deploy failed with a timeout"
        self.recent = self.capture("recent", text, observed=self.last_week[0] + 2 * DAY + 10 * HOUR)
        self.old = self.capture("old", text, observed=self.last_week[0] - 12 * DAY + 10 * HOUR)
        self.other = self.capture("other", text, observed=self.last_week[0] - 40 * DAY + 10 * HOUR)

    def test_a_relative_date_prefers_records_in_its_range_without_filtering(self):
        self.setup_corpus()
        plain = self.brief("deploy failed timeout")
        self.assertEqual(3, len(plain["guidance"]))
        packet = self.brief("deploy failed timeout last week")
        self.assertEqual(3, len(packet["guidance"]), "a date never filters")
        self.assertEqual(self.recent, self.refs(packet)[0])
        self.assertTrue(packet["guidance"][0]["in_time_range"])
        self.assertNotIn("in_time_range", packet["guidance"][1])

    def test_date_words_are_not_search_words(self):
        self.setup_corpus()
        self.capture("week", "weekly sync notes mention a week of planning", observed=self.now)
        packet = self.brief("deploy failed timeout last week")
        self.assertNotIn("planning", json.dumps(packet["guidance"]))
        stages = {t["stage"]: t for t in self.receipt(packet)["trace"]}
        trace = stages["time_v1"]
        self.assertEqual("applied", trace["state"])
        self.assertEqual(["last week"], trace["removed_from_terms"])
        self.assertEqual(self.last_week[0], trace["expressions"][0]["from_ms"])
        self.assertEqual(packet["evaluated_ms"], trace["reference_ms"])
        self.assertIn("last_week_is_the_previous_calendar_week", trace["ambiguity"])

    def test_an_explicit_date_and_a_range_before_it(self):
        self.setup_corpus()
        old_day = stamp(self.last_week[0] - 12 * DAY + 10 * HOUR)
        packet = self.brief(f"deploy failed timeout on {old_day}")
        self.assertEqual(self.old, self.refs(packet)[0])
        packet = self.brief(f"deploy failed timeout before {stamp(self.last_week[0])}")
        self.assertEqual(self.recent, self.refs(packet)[-1])
        self.assertEqual({self.old, self.other}, set(self.refs(packet)[:2]))

    def test_the_reference_instant_is_the_as_of_time(self):
        self.setup_corpus()
        later = self.now + 10 * DAY
        yesterday = resolve("yesterday", later).ranges[0]
        mark = self.capture(
            "marked", "the deploy failed with a timeout", observed=yesterday[0] + HOUR
        )
        packet = self.brief("deploy failed timeout yesterday", as_of_ms=later)
        self.assertEqual(mark, self.refs(packet)[0])
        stages = {t["stage"]: t for t in self.receipt(packet)["trace"]}
        self.assertEqual(later, stages["time_v1"]["reference_ms"])

    def test_a_query_with_no_time_ranks_as_before(self):
        self.setup_corpus()
        packet = self.brief("deploy failed timeout")
        stages = {t["stage"]: t for t in self.receipt(packet)["trace"]}
        self.assertEqual("no_time_in_query", stages["time_v1"]["state"])
        self.assertTrue(all("in_time_range" not in g for g in packet["guidance"]))

    def test_an_explicit_time_range_is_a_hard_filter(self):
        self.setup_corpus()
        around_old = {
            "from_ms": self.last_week[0] - 13 * DAY,
            "to_ms": self.last_week[0] - 11 * DAY,
        }
        packet = self.brief("deploy failed timeout", time_range=around_old)
        self.assertEqual([self.old], self.refs(packet))
        open_ended = self.brief("deploy failed timeout", time_range={"from_ms": self.last_week[0]})
        self.assertEqual([self.recent], self.refs(open_ended))
        stages = {t["stage"]: t for t in self.receipt(packet)["trace"]}
        self.assertEqual("hard", stages["time_v1"]["explicit_range"]["filter"])

    def test_an_explicit_range_applies_before_the_candidate_limit(self):
        # 300 newer matches would crowd the one old match out of a newest-first window.
        self.capture("ancient", "zebra migration notes", observed=1_000_000)
        for n in range(300):
            self.capture(f"filler{n}", "zebra filler line", observed=2_000_000 + n)
        packet = self.brief("zebra", time_range={"from_ms": 0, "to_ms": 1_500_000})
        self.assertEqual(1, len(packet["guidance"]))
        self.assertIn("migration", packet["guidance"][0]["text"])

    def test_the_time_range_is_validated(self):
        for bad in ({}, {"from_ms": -1}, {"from_ms": 1, "to_ms": "x"}, {"until": 3}):
            with self.assertRaises(LocalError) as raised:
                self.brief("zebra", time_range=bad)
            self.assertEqual("invalid_request", raised.exception.code)

    def test_a_query_of_only_a_date_still_searches_something(self):
        self.setup_corpus()
        self.capture("w", "the week review", observed=self.now)
        packet = self.brief("last week")
        self.assertEqual("partial", packet["status"])
        self.assertEqual(1, len(packet["guidance"]))


class LocalTimeBriefingTests(TimeBriefingCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown


class PostgresTimeBriefingTests(TimeBriefingCases, pg.NativeFixture):
    pass


if __name__ == "__main__":
    unittest.main()
