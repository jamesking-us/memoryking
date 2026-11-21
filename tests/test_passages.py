"""Passages and one answer per fact: quoting the best passage of a long observation,
ranking per passage, and collapsing duplicates and superseded versions in a briefing.

The domain cases pin the passage rule; the store cases run on SQLite and on native
PostgreSQL, so both engines must index, quote, collapse and purge the same way.
"""

import json
import unittest

import test_knowledge as knowledge
import test_postgres_parity as pg
from test_lexical_search import LexicalCases

from memoryking.adapters.retrieval import Retrieval
from memoryking.application import lexical_index
from memoryking.domain.briefing import PREVIOUS_CONFIGURATION
from memoryking.domain.duplicates import duplicates, family, shape
from memoryking.domain.lexicon import index_terms
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

    def test_the_score_is_the_best_passage_damped_by_the_size_of_the_observation(self):
        from memoryking.domain.lexicon import observation_score

        self.assertEqual(0, observation_score([]))
        self.assertEqual(0, observation_score([0, 0]))
        self.assertEqual(300_000, observation_score([300_000]))
        two = observation_score([100_000, 300_000])
        many = observation_score([0] * 24 + [300_000])
        self.assertLess(many, two)
        self.assertLess(two, 300_000)
        self.assertGreater(many, 300_000 // 4)
        self.assertEqual(two, observation_score([300_000, 100_000]))

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
        self.assertEqual(family("a", "s", "name"), family("a", "s", "name__v2"))
        self.assertNotEqual(family("a", "s", "name"), family("b", "s", "name"))


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

    def test_a_long_capture_holding_the_answer_is_found_among_short_notes(self):
        # Scored as one document, a 25 KB capture would be all but invisible next to short
        # notes; scored by its best passage (damped by its size) it is found and quoted.
        target = self.capture(
            "long", long_note("The actual finding: the zukatori flag broke the rebalance.")
        )
        for number in range(3):
            self.capture(f"s{number}", f"Short note {number} about the rebalance meeting.")
            self.capture(f"z{number}", f"Short note {number} about a zukatori sticker.")
        packet = self.brief("zukatori flag rebalance")
        self.assertIn(target, self.refs(packet))
        item = next(g for g in packet["guidance"] if g["reference"] == target)
        self.assertIn("zukatori flag broke the rebalance", item["text"])

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

    def recomputed_by_passage(self):
        tenant = self.tenant_id()
        terms: dict = {}
        projects: dict = {}
        for row in self.store.db.execute(
            "SELECT o.project,l.text FROM lexical l JOIN observations o ON o.tenant=l.tenant AND o.id=l.id WHERE l.tenant=? AND o.deleted=0",
            (tenant,),
        ).fetchall():
            for part in (p for p in row[1].split("\n") if p.strip()):
                found, length = index_terms(part)
                count, tokens = projects.get(row[0], (0, 0))
                projects[row[0]] = (count + 1, tokens + length)
                for term in found:
                    terms[(row[0], term)] = terms.get((row[0], term), 0) + 1
        return terms, projects

    def test_statistics_count_passages_and_follow_capture_purge_and_reindex(self):
        long = self.capture("long", long_note())
        short = self.capture("short", "Gateway timeout is thirty seconds.")
        passages = len(split(long_note()))
        self.assertGreater(passages, 5)
        self.assertEqual(passages + 1, self.documents())
        self.assertEqual(self.recomputed_by_passage(), self.statistics())
        self.store.purge(self.owner, long)
        self.assertEqual(1, self.documents())
        self.assertEqual(
            0,
            self.store.db.execute("SELECT count(*) FROM lexical WHERE id=?", (long,)).fetchone()[0],
        )
        self.assertEqual(self.recomputed_by_passage(), self.statistics())
        again = self.capture("again", long_note())
        before = self.statistics()
        self.store.reindex(self.owner)
        self.assertEqual(before, self.statistics())
        self.assertEqual(self.recomputed_by_passage(), self.statistics())
        self.store.purge(self.owner, again)
        self.store.purge(self.owner, short)
        self.assertEqual({}, {k: v for k, v in self.statistics()[0].items() if k[0] == "q"})
        self.assertEqual(0, self.documents())

    def test_a_held_long_observation_is_invisible_in_every_passage(self):
        from memoryking.adapters.trust import Trust

        held = self.capture("held", long_note("The actual finding: the zukatori flag broke it."))
        self.assertIn(held, self.refs(self.brief("zukatori flag")))
        with self.store.transaction() as decision:
            Trust(self.store).hold(self.tenant_id(), "q", held, "scraper", decision.evaluated_ms)
        self.assertEqual([], self.refs(self.brief("zukatori flag")))
        self.assertEqual([], self.refs(self.brief("service request filler")))

    def test_a_store_without_passages_keeps_working_until_reindex(self):
        old = self.capture("long", long_note())
        self.capture("note", "Gateway timeout is thirty seconds.")
        tenant = self.tenant_id()
        rows = self.store.db.execute(
            "SELECT o.id,o.blob,o.project FROM observations o WHERE o.tenant=? AND o.deleted=0",
            (tenant,),
        ).fetchall()
        self.store.db.execute("DELETE FROM lexical_terms WHERE tenant=?", (tenant,))
        self.store.db.execute("DELETE FROM lexical_projects WHERE tenant=?", (tenant,))
        from memoryking.domain.lexicon import index_text

        for row in rows:
            text = lexical_index.blob_text(self.store.root, row, tenant)
            whole = index_text(text)
            self.store.db.execute(
                "UPDATE lexical SET text=? WHERE tenant=? AND id=?", (whole, tenant, row[0])
            )
            lexical_index._count(self.store.db, tenant, row[2], [whole], 1)
        self.store.db.execute(
            "UPDATE lexical_state SET configuration=? WHERE tenant=?",
            (PREVIOUS_CONFIGURATION, tenant),
        )
        status = self.store.status(self.owner, "q")["lexical"]
        self.assertEqual("current", status["state"])
        self.assertEqual(PREVIOUS_CONFIGURATION, status["configuration"])
        self.assertIn("reindex", status["action"])
        self.assertEqual(2, self.documents())
        packet = self.brief("service kafka rebalance storm")
        self.assertEqual(PREVIOUS_CONFIGURATION, self.receipt(packet)["configuration"])
        item = next(g for g in packet["guidance"] if g["reference"] == old)
        self.assertIn("kafka consumer lag", item["text"])
        # a capture and a purge keep the old format exact
        fresh = self.capture("fresh", "Gateway timeout raised to sixty seconds.")
        self.assertEqual(3, self.documents())
        self.store.purge(self.owner, fresh)
        self.assertEqual(2, self.documents())
        self.store.reindex(self.owner)
        status = self.store.status(self.owner, "q")["lexical"]
        self.assertEqual("lexical-v3", status["configuration"])
        self.assertEqual(self.recomputed_by_passage(), self.statistics())
        self.assertEqual("lexical-v3", self.receipt(self.brief("gateway timeout"))["configuration"])

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
