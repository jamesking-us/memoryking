"""Passages and one answer per fact: quoting the best passage of a long observation,
ranking per passage, and collapsing duplicates and superseded versions in a briefing.

The domain cases pin the passage rule; the store cases run on SQLite and on native
PostgreSQL, so both engines must index, quote, collapse and purge the same way.
"""

import json
import re
import unittest
from unittest import mock

import test_knowledge as knowledge
import test_postgres_parity as pg
from test_lexical_search import LexicalCases

from memoryking.adapters import retrieval
from memoryking.adapters.retrieval import TEXT_EXCERPT_BYTES, Retrieval
from memoryking.domain.duplicates import (
    contained,
    duplicate_clusters,
    duplicates,
    family,
    holders,
    shape,
)
from memoryking.domain.passages import EXCERPT_BYTES, MAXIMUM, TAIL, excerpt, split


def long_note(finding="The actual finding: kafka consumer lag came from the rebalance storm."):
    return (
        "The service handled the request. " * 330
        + finding
        + " "
        + "More service request filler. " * 190
    )


class PassageRuleTests(unittest.TestCase):
    def check(self, text):
        passages = split(text)
        self.assertEqual(0, passages[0].start)
        self.assertEqual(len(text), passages[-1].end)
        raw = text.encode()
        for before, after in zip(passages, passages[1:], strict=False):
            self.assertEqual(before.end, after.start)
            self.assertEqual(before.end_byte, after.start_byte)
        for passage in passages:
            self.assertEqual(
                text[passage.start : passage.end].encode(),
                raw[passage.start_byte : passage.end_byte],
            )
        return passages

    def test_a_short_observation_is_one_passage(self):
        for text in ("a", "Gateway timeout is thirty seconds.", "x. " * 400):
            self.assertEqual(1, len(self.check(text)))
        self.assertEqual([], split(""))

    def test_passages_cover_the_text_in_order_with_exact_byte_offsets(self):
        text = long_note("Naïve café résumé 日本語。") + "\n\n" + "Ünïcode paragraph. " * 100
        passages = self.check(text)
        self.assertGreater(len(passages), 10)
        for passage in passages[:-1]:
            self.assertLessEqual(passage.end - passage.start, MAXIMUM)

    def test_the_same_text_always_gives_the_same_passages(self):
        text = long_note()
        self.assertEqual(split(text), split(text))

    def test_passages_end_at_sentence_or_paragraph_boundaries(self):
        text = long_note()
        for passage in split(text)[:-1]:
            self.assertTrue(text[passage.start : passage.end].rstrip().endswith("."))
        paragraphs = ("Alpha beta gamma. " * 40 + "\n\n") * 6
        for passage in split(paragraphs)[:-1]:
            self.assertTrue(paragraphs[passage.end - 1].isspace())

    def test_a_code_or_path_line_is_never_cut(self):
        code = "def retry_charge(customer):\n    return gateway.charge(customer.id, retries=3)\n"
        path = "See src/memoryking/adapters/retrieval.py. Then src/memoryking/domain/passages.py. "
        text = ("Prose about the incident. " * 30 + "\n") + (code * 20) + (path * 10 + "\n")
        text += "More prose. " * 200
        boundaries = {p.end for p in split(text)}
        offset = 0
        guarded = 0
        for line in text.splitlines(keepends=True):
            if line.startswith(("def ", "    return", "See src")):
                guarded += 1
                inside = [b for b in boundaries if offset < b < offset + len(line)]
                self.assertEqual([], inside, line[:30])
            offset += len(line)
        self.assertGreater(guarded, 20)
        self.assertGreater(len(boundaries), 3)

    def test_an_overlong_run_is_cut_at_whitespace_or_at_the_limit(self):
        text = "word " * 1000
        for passage in self.check(text):
            self.assertLessEqual(passage.end - passage.start, MAXIMUM + TAIL)
        solid = "x" * 5000
        self.assertTrue(all(p.end - p.start <= MAXIMUM + TAIL for p in self.check(solid)))
        self.assertTrue(all(text[p.start - 1] == " " for p in split(text)[1:]))

    def test_the_excerpt_is_the_best_passage_and_stays_inside_the_limit(self):
        text = long_note()
        passages = split(text)
        scores = [0] * len(passages)
        scores[12] = 500
        first, last = excerpt(text, passages, scores)
        self.assertLessEqual(len(text[first:last].encode()), EXCERPT_BYTES)
        self.assertTrue(passages[12].start <= first and last <= passages[12].end)
        self.assertEqual(text[first:last], text[first:last].strip())
        # a neighbour that scores nearly as well joins it, one that does not stays out
        scores[13], scores[11] = 450, 100
        first, last = excerpt(text, passages, scores)
        self.assertEqual(passages[12].start, first)
        self.assertGreater(last, passages[12].end - 1)
        self.assertLessEqual(last, passages[13].end)
        self.assertLess(passages[12].start, passages[13].end)
        self.assertNotEqual(passages[11].start, first)
        # a short text is quoted whole
        self.assertEqual((0, 5), excerpt("hello", split("hello"), [3]))

    def test_a_long_capture_scores_as_its_best_passage_when_that_beats_the_whole(self):
        from memoryking.domain.lexicon import RESCUE_PASSAGES, rescued

        short = [10] * RESCUE_PASSAGES  # not long enough: the whole-text score stands
        self.assertEqual(100, rescued(100, [*short[:-1], 900]))
        long = [10] * (RESCUE_PASSAGES + 1)
        self.assertEqual(900, rescued(100, [*long[:-1], 900]))
        self.assertEqual(1000, rescued(1000, [*long[:-1], 900]))

    def test_duplicates_are_conservative(self):
        base = "Flaky test test_checkout_retry failed again on CI run {}: AssertionError timeout."
        self.assertTrue(duplicates(shape(base.format(1)), shape(base.format(2))))
        self.assertTrue(
            duplicates(
                shape("The limit is  Thirty requests. "), shape("the limit is thirty requests.")
            )
        )
        self.assertFalse(
            duplicates(
                shape("The gateway limit is thirty requests."),
                shape("The gateway limit is sixty requests."),
            )
        )
        long_a = "Payments retry failed on staging for the checkout service; see {} for details of the cause."
        self.assertFalse(
            duplicates(shape(long_a.format("PAY-142")), shape(long_a.format("PAY-143")))
        )
        self.assertFalse(
            duplicates(
                shape(
                    "Deploys to production are allowed on Fridays after the freeze review is done."
                ),
                shape(
                    "Deploys to production are not allowed on Fridays after the freeze review is done."
                ),
            )
        )
        chatty = "After the review the whole team agreed that from now on the {} channel is {} for every environment."
        self.assertFalse(
            duplicates(
                shape(chatty.format("vezudo", "kaso")), shape(chatty.format("rodote", "mune"))
            )
        )
        self.assertFalse(
            duplicates(
                shape(chatty.format("vezudo", "kaso")), shape(chatty.format("vezudo", "mune"))
            )
        )
        self.assertEqual(("a", "s", "name"), family("a", "s", "name__v7"))

    def test_contained_needs_nearly_all_of_the_shorter_text_and_its_anchors(self):
        said = "User: Please tidy the orders export before Friday.\n\nUser: Delimiter is a caret."
        whole = (
            "User: Please tidy the orders export before Friday.\n\n"
            "Assistant: Opening ledger/exporter.py first.\n\n"
            "User: Delimiter is a caret.\n\nAssistant: Done."
        )
        self.assertTrue(contained(shape(said), shape(whole)))
        self.assertFalse(contained(shape(whole), shape(said)))  # never the other way
        self.assertFalse(contained(shape(whole), shape(whole)))  # equal text is a duplicate
        # a paragraph the longer text lacks (more than a tenth of the shorter) breaks it
        self.assertFalse(
            contained(shape(said + "\n\nUser: And rename the helper too."), shape(whole))
        )
        # too few words to say they are the same record
        self.assertFalse(contained(shape("User: Done."), shape("User: Done.\n\nAssistant: OK.")))
        # inside a longer name is not the same name: the anchors must match
        near = "Please look at {} before the Friday release of the exporter now"
        self.assertFalse(
            contained(
                shape(near.format("ledger/exporter.py")),
                shape(near.format("src/ledger/exporter.py") + ", thanks."),
            )
        )
        self.assertTrue(
            contained(
                shape(near.format("ledger/exporter.py")),
                shape(near.format("ledger/exporter.py") + ", thanks."),
            )
        )
        self.assertEqual(family("a", "s", "name"), family("a", "s", "name__v2"))
        self.assertNotEqual(family("a", "s", "name"), family("b", "s", "name"))


class RepeatSearchTests(unittest.TestCase):
    """`duplicate_clusters` and `holders` give what comparing every pair gives, faster."""

    def sessions(self, count=60):
        import random

        rng = random.Random(7)
        words = "orders export delimiter semicolon header casing encoding retry gateway".split()
        out = []
        for n in range(count):
            turns = [f"User: {' '.join(rng.sample(words, 8))} request number {n % 7}"]
            turns += [f"Assistant: {' '.join(rng.choices(words, k=9))} run {rng.randint(1, 4)}"]
            body = "\n\n".join(turns)
            out.append(body)
            if n % 3 == 0:  # the user's own turns on their own: inside the whole capture
                out.append(turns[0])
            if n % 5 == 0:  # the same text with another number
                out.append(body.replace("run 1", "run 2"))
        return out

    def test_clusters_are_those_of_comparing_every_excerpt_with_every_first_member(self):
        shapes = [shape(t) for t in self.sessions()]
        keys = [n // 10 % 2 for n in range(len(shapes))]  # two "time ranges" kept apart
        expected: list[list[int]] = []
        for index, mine in enumerate(shapes):
            for cluster in expected:
                if keys[cluster[0]] == keys[index] and duplicates(shapes[cluster[0]], mine):
                    cluster.append(index)
                    break
            else:
                expected.append([index])
        self.assertEqual(expected, duplicate_clusters(shapes, keys))
        self.assertTrue(any(len(c) > 1 for c in expected))

    def test_holders_are_the_shortest_excerpt_that_holds_each_other_one(self):
        shapes = [shape(t) for t in self.sessions()]
        keys = [n // 10 % 2 for n in range(len(shapes))]
        expected: dict[int, int] = {}
        for index, small in enumerate(shapes):
            best = None
            for other, big in enumerate(shapes):
                if other != index and keys[other] == keys[index] and contained(small, big):
                    if best is None or len(big.text) < best[0]:
                        best = (len(big.text), other)
            if best is not None:
                expected[index] = best[1]
        self.assertEqual(expected, holders(shapes, keys))
        self.assertGreater(len(expected), 3)


class PassageCases(LexicalCases):
    def fetch(self, reference, start, end):
        output = []
        Retrieval(self.store).execute(
            self.owner,
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": "fetch",
                "args": {
                    "project": "q",
                    "reference": reference,
                    "kind": "observation",
                    "start_byte": start,
                    "end_byte": end,
                    "max_output_tokens": 6000,
                    "tokenizer": "utf8-byte-estimate-v1",
                },
            },
        ).emit(output.append)
        return json.loads(output[0])

    def replay(self, packet):
        output = []
        Retrieval(self.store).execute(
            self.owner,
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": "replay",
                "args": {"project": "q", "receipt": packet["receipt"]},
            },
        ).emit(output.append)
        return output[0]

    def documents(self):
        return self.statistics()[1].get("q", (0, 0))[0]

    # --- quoting the best passage -------------------------------------------------------

    def test_the_briefing_quotes_the_passage_that_holds_the_answer(self):
        target = self.capture("long", long_note())
        self.capture("note", "Unrelated hiring plan.")
        packet = self.brief("service kafka rebalance storm")
        item = next(g for g in packet["guidance"] if g["reference"] == target)
        self.assertIn("kafka consumer lag", item["text"])
        self.assertLessEqual(item["excerpt_end_byte"] - item["excerpt_start_byte"], EXCERPT_BYTES)
        self.assertEqual(len(long_note().encode()), item["source_bytes"])
        # the excerpt starts and ends on word boundaries, and fetch returns exactly it
        self.assertEqual(item["text"], item["text"].strip())
        fetched = self.fetch(target, item["excerpt_start_byte"], item["excerpt_end_byte"])
        self.assertEqual(item["text"], fetched["excerpt"])
        self.assertEqual(item["source_bytes"], fetched["source_bytes"])
        self.assertEqual(1, [g["reference"] for g in packet["guidance"]].count(target))

    def test_a_text_briefing_quotes_a_long_observation_more_briefly(self):
        # A text briefing that names no count is limited by its budget, so a long record is
        # quoted in fewer bytes (and more records fit); naming a count keeps the full quote.
        target = self.capture("long", "Kafka rebalance storm notes for the service. " * 400)
        self.capture("note", "Unrelated hiring plan.")
        query = "service kafka rebalance storm"
        structured = next(g for g in self.brief(query)["guidance"] if g["reference"] == target)
        full = structured["excerpt_end_byte"] - structured["excerpt_start_byte"]
        self.assertGreater(full, TEXT_EXCERPT_BYTES)
        sentence = "Kafka rebalance storm notes for the service."
        shown = {}
        for name, extra in (("text", {}), ("counted", {"top_k": 8})):
            text = self.brief(query, format="text", **extra)["text"]
            shown[name] = text.count(sentence) * len(sentence)
        self.assertLessEqual(shown["text"], TEXT_EXCERPT_BYTES)
        self.assertGreater(shown["counted"], TEXT_EXCERPT_BYTES)

    def test_a_short_record_is_shown_while_long_ones_above_it_are_quoted_briefly(self):
        # The first two records keep the long quote; every later one is first taken short
        # (a long record by its first 600 bytes), and what the budget leaves widens them in
        # rank order. A well-ranked short record is therefore not dropped for the budget
        # while long records above it take thousands of bytes.
        longs = [
            self.capture(f"long{k}", f"Kafka rebalance storm notes for the {w} service. " * 60)
            for k, w in enumerate(("alpha", "beta", "gamma"))
        ]
        actions = (
            "restart the consumer",
            "raise the timeout",
            "pin partitions",
            "drain the broker",
        )
        shorts = [
            self.capture(f"s{n}", f"Kafka rebalance storm, service note {n}: {action}.")
            for n, action in enumerate(actions)
        ]
        query = "kafka rebalance storm service notes"
        budget = 7000

        def shown(**extra):
            packet = self.brief(query, format="text", max_output_tokens=budget, **extra)
            return packet, [source["id"] for source in packet["sources"]]

        packet, ids = shown()
        self.assertEqual(set(longs + shorts), set(ids))
        self.assertLessEqual(packet["usage"]["output_tokens"], budget)
        again = self.brief(query, format="text", max_output_tokens=budget)
        self.assertEqual(packet["text"].split("\n", 2)[2:], again["text"].split("\n", 2)[2:])
        with mock.patch.object(retrieval, "TEXT_FIRST_BYTES", 0):
            _, before = shown()
        self.assertLess(len(before), len(ids))  # without the short first allocation
        # a count keeps the full quote of every long record
        counted = self.brief(query, top_k=8)["guidance"]
        for item in counted:
            if item["reference"] in longs:
                size = item["excerpt_end_byte"] - item["excerpt_start_byte"]
                self.assertGreater(size, 2000)

    def test_counted_and_structured_briefings_do_not_take_the_short_first_allocation(self):
        for k in range(4):
            self.capture(f"long{k}", f"Kafka rebalance storm notes, part {k}. " * 80)
        query = "kafka rebalance storm notes"

        def body(packet):
            return [
                (g["reference"], g["text"], g["excerpt_start_byte"]) for g in packet["guidance"]
            ]

        def kept(**extra):
            packet = self.brief(query, **extra)
            return packet["text"].split("\n", 2)[2:] if "text" in packet else body(packet)

        for extra in ({}, {"top_k": 8}, {"format": "text", "top_k": 8, "max_output_tokens": 6000}):
            with mock.patch.object(retrieval, "TEXT_FIRST_BYTES", 0):
                before = kept(**extra)
            self.assertEqual(before, kept(**extra))

    def test_a_long_capture_holding_the_answer_is_not_buried_by_its_length(self):
        # Scored as one document, a 25 KB capture is all but invisible next to short notes;
        # its best passage rescues it, and the quote is that passage.
        target = self.capture(
            "long", long_note("The actual finding: the zukatori flag broke the rebalance.")
        )
        for number in range(6):
            self.capture(f"s{number}", f"Short note {number} about the rebalance meeting.")
            self.capture(f"z{number}", f"Short note {number} about a zukatori sticker.")
        packet = self.brief("zukatori flag rebalance")
        self.assertEqual(target, self.refs(packet)[0])
        self.assertIn("zukatori flag broke the rebalance", packet["guidance"][0]["text"])

    def test_a_short_observation_is_quoted_whole(self):
        text = "Gateway timeout is thirty seconds.\nRetry twice, then page the owner."
        target = self.capture("short", text)
        item = self.brief("gateway timeout")["guidance"][0]
        self.assertEqual(target, item["reference"])
        self.assertEqual(text, item["text"])
        self.assertEqual((0, len(text)), (item["excerpt_start_byte"], item["excerpt_end_byte"]))

    def test_an_anchor_in_the_query_steers_the_quote_to_the_passage_naming_it(self):
        text = "Routine filler sentence. " * 300 + "Fixed in PAY-142 by pinning the driver. "
        text += "Routine filler sentence. " * 300
        target = self.capture("anchored", text)
        packet = self.brief("PAY-142")
        item = next(g for g in packet["guidance"] if g["reference"] == target)
        self.assertIn("PAY-142", item["text"])

    # --- statistics, purge, holds, old stores -------------------------------------------

    def test_a_held_long_observation_is_invisible_in_every_passage(self):
        from memoryking.adapters.trust import Trust

        held = self.capture("held", long_note("The actual finding: the zukatori flag broke it."))
        self.assertIn(held, self.refs(self.brief("zukatori flag")))
        with self.store.transaction() as decision:
            Trust(self.store).hold(self.tenant_id(), "q", held, "scraper", decision.evaluated_ms)
        self.assertEqual([], self.refs(self.brief("zukatori flag")))
        self.assertEqual([], self.refs(self.brief("service request filler")))

    # --- one answer per fact --------------------------------------------------------------

    def test_near_identical_notes_return_as_one_item_that_names_the_others(self):
        notes = [
            self.capture(
                f"f{n}",
                f"Flaky test test_checkout_retry failed again on CI run {n}: AssertionError timeout.",
            )
            for n in range(5)
        ]
        other = self.capture("other", "test_checkout_retry now has a ten second timeout budget.")
        packet = self.brief("test_checkout_retry AssertionError timeout")
        refs = self.refs(packet)
        kept = [r for r in refs if r in notes]
        self.assertEqual([notes[-1]], kept)  # the newest record's text
        item = next(g for g in packet["guidance"] if g["reference"] == notes[-1])
        self.assertEqual(4, item["also_recorded_in"]["count"])
        self.assertEqual(set(notes[:-1]), set(item["also_recorded_in"]["references"]))
        self.assertIn("run 4", item["text"])
        self.assertIn(other, refs)
        # the receipt still depends on every collapsed record, so a purge retires the briefing
        self.assertIsInstance(self.replay(packet), bytes)
        self.store.purge(self.owner, notes[1])
        from memoryking.domain.local import LocalError

        with self.assertRaises(LocalError):
            self.replay(packet)
        trace = next(
            t
            for t in self.receipt(self.brief("test_checkout_retry timeout"))["trace"]
            if t["stage"] == "one_answer_per_fact"
        )
        self.assertEqual(3, trace["duplicates_collapsed"])

    def test_the_same_sentence_on_two_days_and_with_a_trailing_space_is_one_item(self):
        first = self.capture("day1", "Deploys need a second reviewer.", observed=1_000)
        second = self.capture("day2", "Deploys need a second reviewer. ", observed=90_000_000)
        third = self.capture("day3", "deploys   need a second\nreviewer.", observed=180_000_000)
        packet = self.brief("deploys reviewer")
        self.assertEqual([third], self.refs(packet))
        found = packet["guidance"][0]["also_recorded_in"]
        self.assertEqual({first, second}, set(found["references"]))
        self.assertEqual(2, found["count"])

    def test_notes_with_different_anchors_are_never_collapsed(self):
        text = "Payments retry failed on staging for the checkout service; see {} for the cause details."
        a = self.capture("a", text.format("PAY-142"))
        b = self.capture("b", text.format("PAY-143"))
        c = self.capture(
            "c", "Edited src/app/main.py to retry the charge on the checkout service twice."
        )
        d = self.capture(
            "d", "Edited src/app/util.py to retry the charge on the checkout service twice."
        )
        self.assertLessEqual({a, b}, set(self.refs(self.brief("payments retry failed staging"))))
        self.assertLessEqual(
            {c, d}, set(self.refs(self.brief("edited retry charge checkout twice")))
        )

    def test_versions_of_one_file_show_only_the_newest_and_name_the_older(self):
        v1 = self.capture("plan", "Release plan: ship the exporter in March after the audit.")
        v2 = self.capture("plan__v2", "Release plan: ship the exporter in April after the audit.")
        v3 = self.capture("plan__v3", "Release plan: ship the exporter in June, audit moved.")
        other = self.capture("other", "Release plan for the dashboard is unrelated.")
        packet = self.brief("release plan exporter")
        refs = self.refs(packet)
        self.assertIn(v3, refs)
        self.assertNotIn(v1, refs)
        self.assertNotIn(v2, refs)
        item = next(g for g in packet["guidance"] if g["reference"] == v3)
        self.assertEqual({v1, v2}, set(item["supersedes"]["references"]))
        self.assertEqual(2, item["supersedes"]["count"])
        self.assertIn(other, self.brief("release plan dashboard")["guidance"][0]["reference"])
        self.replay(packet)
        self.store.purge(self.owner, v1)
        from memoryking.domain.local import LocalError

        with self.assertRaises(LocalError):
            self.replay(packet)

    def test_collapsing_makes_room_for_other_results_within_top_k(self):
        for n in range(6):
            self.capture(
                f"d{n}", f"Flaky checkout test failed again on CI run {n} with a timeout error."
            )
        texts = [
            "Checkout test coverage documents the failed retry timeout path.",
            "Timeouts in checkout tests are retried; failed attempts are logged.",
            "The checkout test harness fails when a timeout budget is missing.",
        ]
        others = [self.capture(f"o{n}", text) for n, text in enumerate(texts)]
        refs = self.refs(self.brief("checkout test failed timeout", top_k=4))
        self.assertEqual(4, len(refs))
        self.assertTrue(set(others) <= set(refs))

    def test_an_excerpt_inside_another_is_shown_once_as_the_longer_text(self):
        # What a hook captures: the session, and the user's turns of it on their own.
        said = (
            "User: Please tidy the orders export before Friday.\n\n"
            "User: Settled in the team call: orders export column delimiter is a caret."
        )
        session = (
            "User: Please tidy the orders export before Friday.\n\n"
            "Assistant: Opening ledger/exporter.py first.\n\n"
            "User: Settled in the team call: orders export column delimiter is a caret.\n\n"
            "Assistant: Done, and the tests pass."
        )
        mine = self.capture("said", said)
        whole = self.capture("session", session)
        self.capture("other", "Unrelated: the dashboard colour palette was changed on Tuesday.")
        packet = self.brief("orders export column delimiter caret")
        self.assertEqual([whole], self.refs(packet))
        item = packet["guidance"][0]
        self.assertEqual([mine], item["also_recorded_in"]["references"])
        self.assertIn("Assistant: Done", item["text"])  # the longer text, verbatim
        trace = next(
            t for t in self.receipt(packet)["trace"] if t["stage"] == "one_answer_per_fact"
        )
        self.assertEqual(1, trace["contained_collapsed"])
        # The receipt depends on the contained record too: purging it retires the briefing.
        self.assertIsInstance(self.replay(packet), bytes)
        self.store.purge(self.owner, mine)
        from memoryking.domain.local import LocalError

        with self.assertRaises(LocalError):
            self.replay(packet)

    def test_a_held_contained_excerpt_is_neither_shown_nor_named(self):
        from memoryking.adapters.trust import Trust

        said = "User: Please tidy the orders export before Friday, the delimiter is a caret."
        session = said + "\n\nAssistant: Done, and the tests pass for the orders export."
        mine = self.capture("said", said)
        whole = self.capture("session", session)
        with self.store.transaction() as decision:
            Trust(self.store).hold(self.tenant_id(), "q", mine, "scraper", decision.evaluated_ms)
        packet = self.brief("orders export delimiter caret")
        self.assertEqual([whole], self.refs(packet))
        self.assertNotIn("also_recorded_in", packet["guidance"][0])

    def test_an_excerpt_that_is_only_partly_inside_another_stays_separate(self):
        said = "User: Please tidy the orders export before Friday. Also rename the helper."
        session = (
            "User: Please tidy the orders export before Friday.\n\n"
            "Assistant: Opening ledger/exporter.py first, and the delimiter is a caret."
        )
        a = self.capture("said", said)
        b = self.capture("session", session)
        self.assertEqual({a, b}, set(self.refs(self.brief("orders export tidy delimiter"))))

    def test_a_text_briefing_holds_as_many_items_as_fit_the_budget(self):
        notes = [
            self.capture(f"n{n}", f"Note {n}: the billing exporter {word} handles retries.")
            for n, word in enumerate(
                "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima".split()
            )
        ]
        everything = self.brief("billing exporter retries", format="text")
        self.assertEqual(len(notes), len(everything["sources"]))
        top3 = self.brief("billing exporter retries", format="text", top_k=3)
        self.assertEqual(3, len(top3["sources"]))
        # The structured packet keeps its small default, and a small budget still bounds the text.
        self.assertEqual(8, len(self.brief("billing exporter retries")["guidance"]))
        small = self.brief(
            "billing exporter retries",
            format="text",
            max_output_tokens=500,
            tokenizer="chars-estimate-v1",
        )
        self.assertLessEqual(small["usage"]["output_tokens"], 500)
        self.assertTrue(0 < len(small["sources"]) < 12)

    def test_a_score_floor_leaves_out_weak_matches_and_the_trace_counts_them(self):
        strong = self.capture("s", "The billing exporter retries failed charges twice.")
        weak = self.capture("w", "Billing statements are sent monthly by the finance team.")
        everything = self.brief("billing exporter retries failed charges")
        self.assertEqual({strong, weak}, set(self.refs(everything)))
        floor = max(
            g["lexical_score_ppm"] for g in everything["guidance"] if g["reference"] == weak
        )
        packet = self.brief("billing exporter retries failed charges", min_score_ppm=floor + 1)
        self.assertEqual([strong], self.refs(packet))
        trace = next(
            t for t in self.receipt(packet)["trace"] if t["stage"] == "one_answer_per_fact"
        )
        self.assertEqual(1, trace["below_floor"])
        self.assertEqual([], self.refs(self.brief("billing exporter", min_score_ppm=1_000_000)))

    def test_a_crowd_of_near_identical_statements_does_not_push_out_different_ones(self):
        values = [f"qa{chr(97 + n)}x" for n in range(20)]
        crowd = [
            self.capture(
                f"c{n}", f"The orders export, column delimiter: {word} (settled in the team call)."
            )
            for n, word in enumerate(values)
        ]
        different = [
            self.capture(
                "d1", "The integration notes say the orders export column delimiter is in UTF-16."
            ),
            self.capture(
                "d2", "Assistant: I checked the column delimiter and header of the orders export."
            ),
        ]
        packet = self.brief("orders export column delimiter", top_k=6)
        refs = self.refs(packet)
        self.assertEqual(6, len(refs))
        self.assertTrue(set(different) <= set(refs), refs)
        self.assertLessEqual(len(set(crowd) & set(refs)), 4)
        trace = next(
            t for t in self.receipt(packet)["trace"] if t["stage"] == "one_answer_per_fact"
        )
        self.assertGreater(trace["diversified"], 0)

    def test_the_first_two_near_identical_statements_keep_their_place_in_a_short_list(self):
        crowd = [
            self.capture(
                f"c{n}",
                f"The orders export, column delimiter: {word} (settled in the team call).",
            )
            for n, word in enumerate(["caret", "pipe", "tilde", "tab", "semicolon", "colon"])
        ]
        different = [
            self.capture("d1", "The integration notes say the orders export is UTF-16 encoded."),
            self.capture("d2", "Assistant: I checked the export header of the orders file today."),
        ]
        refs = self.refs(self.brief("orders export column delimiter", top_k=5))
        self.assertEqual(5, len(refs))
        self.assertTrue(set(different) <= set(refs), refs)
        self.assertLessEqual(len(set(crowd) & set(refs)), 3)

    def test_a_short_list_with_no_crowd_keeps_the_order_of_the_scores(self):
        self.capture("a", "Orders export: the column delimiter is a pipe, settled in the call.")
        self.capture("b", "The orders export column delimiter was discussed in the weekly sync.")
        self.capture("c", "Our orders export writes a header line first, then one line per row.")
        self.capture("d", "Unrelated: the delimiter on the report pages is a tab.")
        packet = self.brief("orders export column delimiter settled", top_k=4)
        scores = [g["lexical_score_ppm"] for g in packet["guidance"]]
        self.assertEqual(sorted(scores, reverse=True), scores)
        trace = next(
            t for t in self.receipt(packet)["trace"] if t["stage"] == "one_answer_per_fact"
        )
        self.assertEqual(0, trace["diversified"])

    def test_a_clearly_better_match_still_comes_before_a_crowd_of_weaker_ones(self):
        best = self.capture("best", "Orders export: the column delimiter is a pipe, final.")
        for n, word in enumerate(["caret", "tilde", "tab", "colon", "semicolon", "comma"]):
            self.capture(
                f"w{n}", f"Notes on the orders export from the {word} meeting, nothing more."
            )
        packet = self.brief("orders export column delimiter pipe", top_k=3)
        self.assertEqual(best, self.refs(packet)[0])

    def crowded(self, count=30):
        """`count` records about one component that differ only in the attribute they state."""
        names = [f"q{chr(97 + n // 5)}{chr(97 + n % 5)}x" for n in range(count)]
        return [
            self.capture(f"r{n:02d}", f"Settled for the orders export: the {name} is {name[::-1]}.")
            for n, name in enumerate(names)
        ]

    def test_pages_of_one_query_follow_each_other_without_a_repeat_or_a_gap(self):
        self.crowded(30)
        whole = self.refs(self.brief("orders export settled", top_k=24, max_output_tokens=60000))
        first = self.refs(self.brief("orders export settled", top_k=8, max_output_tokens=60000))
        second = self.refs(
            self.brief("orders export settled", top_k=8, offset=8, max_output_tokens=60000)
        )
        third = self.refs(
            self.brief("orders export settled", top_k=8, offset=16, max_output_tokens=60000)
        )
        self.assertEqual(24, len(whole))
        self.assertEqual(whole, first + second + third)
        self.assertEqual(24, len(set(first + second + third)))
        trace = next(
            t
            for t in self.receipt(
                self.brief("orders export settled", offset=8, top_k=8, max_output_tokens=60000)
            )["trace"]
            if t["stage"] == "paging"
        )
        self.assertEqual((30, 8, 8), (trace["matched"], trace["offset"], trace["limit"]))
        beyond = self.brief("orders export settled", top_k=8, offset=40, max_output_tokens=60000)
        self.assertEqual([], self.refs(beyond))

    def test_every_word_keeps_only_records_that_hold_all_of_them(self):
        both = self.capture("both", "The orders export column delimiter is a pipe.")
        self.capture("one", "The orders export file encoding is utf-8.")
        self.capture("two", "The report pages column delimiter is a tab.")
        anything = self.refs(self.brief("orders export column delimiter"))
        self.assertEqual(3, len(anything))
        self.assertEqual(
            [both], self.refs(self.brief("orders export column delimiter", match="all"))
        )
        self.assertEqual([], self.refs(self.brief("orders export zebra delimiter", match="all")))
        self.assertEqual(
            [both], self.refs(self.brief("orders export column delimiter", match="any"))[:1]
        )

    def test_a_named_file_keeps_only_records_that_name_it(self):
        main = self.capture("main", "Edited src/app/main.py to retry the charge twice.")
        util = self.capture("util", "Edited src/app/util.py to retry the charge twice.")
        packet = self.brief("edited retry charge twice", file="src/app/main.py")
        self.assertEqual([main], self.refs(packet))
        self.assertEqual({main, util}, set(self.refs(self.brief("edited retry charge twice"))))
        self.assertEqual([], self.refs(self.brief("edited retry charge", file="src/app/none.py")))

    def test_a_long_page_says_what_each_record_names_except_what_all_of_them_name(self):
        refs = [
            self.capture(f"n{n}", f"Changed src/shared.py and src/part{n}.py for the retry policy.")
            for n in range(6)
        ]
        packet = self.brief("changed retry policy", top_k=6)
        by_ref = {g["reference"]: g for g in packet["guidance"]}
        for n, ref in enumerate(refs):
            self.assertEqual([f"src/part{n}.py"], by_ref[ref]["about"])
        text = self.brief("changed retry policy", top_k=6, format="text")["text"]
        self.assertIn("names src/part3.py", text)
        self.assertNotIn("names src/shared.py", text)
        short = self.brief("changed retry policy", top_k=3)
        self.assertTrue(all("about" not in g for g in short["guidance"]))

    def test_a_partial_list_says_where_the_next_records_start_and_how_to_narrow(self):
        self.crowded(30)
        text = self.brief(
            "orders export settled", top_k=10, format="text", max_output_tokens=60000
        )["text"]
        self.assertIn("Partial list: records 1 to 10 of 30 that matched", text)
        self.assertIn("`offset` 10", text)
        self.assertIn("`match` to `all`", text)
        later = self.brief(
            "orders export settled", top_k=10, offset=20, format="text", max_output_tokens=60000
        )["text"]
        self.assertNotIn("Partial list", later)  # the last page: nothing further down
        self.assertEqual(10, later.count("**[g"))
        whole = self.brief(
            "orders export settled", top_k=30, format="text", max_output_tokens=60000
        )["text"]
        self.assertNotIn("Partial list", whole)

    def test_when_more_matched_than_were_compared_the_count_is_a_floor_and_says_so(self):
        for n in range(270):
            name = f"q{chr(97 + n // 26)}{chr(97 + n % 26)}x"
            self.capture(f"m{n:03d}", f"Settled for the orders export: the {name} is {name[::-1]}.")
        text = self.brief("orders export settled", top_k=10, format="text", max_output_tokens=60000)
        self.assertRegex(text["text"], r"records 1 to 10 of at least \d+ that matched")
        self.assertIn("only the newest candidates are", text["text"])
        trace = next(t for t in self.receipt(text)["trace"] if t["stage"] == "lexical")
        self.assertTrue(trace["candidate_limit_reached"])

    def test_a_page_cut_by_the_budget_ends_where_the_next_page_begins(self):
        self.crowded(30)
        first = self.brief("orders export settled", format="text", max_output_tokens=2500)
        shown = [source["id"] for source in first["sources"]]
        self.assertTrue(0 < len(shown) < 30)
        # The turns beside a shown record fill what the budget leaves; the page's own records
        # are the first `offset` of the ordered list.
        offset = int(re.search(r"`offset` (\d+)", first["text"]).group(1))
        self.assertTrue(0 < offset <= len(shown))
        rest = self.brief(
            "orders export settled",
            format="text",
            offset=offset,
            top_k=32,
            max_output_tokens=60000,
        )
        whole = [
            source["id"]
            for source in self.brief(
                "orders export settled", format="text", top_k=32, max_output_tokens=60000
            )["sources"]
        ]
        later = [source["id"] for source in rest["sources"]]
        self.assertLessEqual(set(whole[:offset]), set(shown))
        self.assertEqual(whole[offset:], later[: len(whole) - offset])

    def test_text_rendering_separates_sections_and_shows_the_recorded_minute(self):
        first = self.capture("a", "Deploys need a second reviewer.")
        self.capture("b", "Deploys need a second reviewer.")
        packet = self.brief("deploys reviewer", format="text")
        text = packet["text"]
        self.assertIn("## Unreviewed observations", text)
        self.assertNotIn("## Accepted knowledge", text)
        self.assertIn("also recorded in " + first, text)
        import re

        self.assertRegex(text, r"recorded \d{4}-\d\d-\d\d \d\d:\d\d UTC")
        self.assertIsNone(re.search(r"recorded \d{4}-\d\d-\d\d\)", text))


class LocalPassageTests(PassageCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown


class PostgresPassageTests(PassageCases, pg.NativeFixture):
    pass


if __name__ == "__main__":
    unittest.main()
