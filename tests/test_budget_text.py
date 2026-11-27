"""Budgets count what the model receives: the rendered text, on both backends."""

import json
import unittest

import test_knowledge as knowledge
import test_postgres_parity as pg
from test_lexical_search import LexicalCases

from memoryking.adapters.retrieval import Retrieval
from memoryking.adapters.token_count import counter, estimate

ESTIMATE = "chars-estimate-v1"


class BudgetCases(LexicalCases):
    def ask(self, query, budget, **extra):
        return self.brief(
            query, format="text", max_output_tokens=budget, tokenizer=ESTIMATE, **extra
        )

    def fill(self, count=12):
        for n in range(count):
            self.capture(f"note-{n}", f"the export retry policy note {n} " + "backoff detail " * 40)

    def test_the_budget_governs_the_rendered_text_not_the_envelope(self):
        self.fill()
        count, _ = counter(ESTIMATE)
        for budget in (600, 1200, 2500):
            packet = self.ask("export retry", budget)
            self.assertEqual("text", packet["format"])
            self.assertEqual(count(packet["text"]), packet["usage"]["output_tokens"])
            self.assertLessEqual(packet["usage"]["output_tokens"], budget)
            self.assertEqual("rendered_text", packet["usage"]["counted"])
            self.assertEqual("rendered_text", self.receipt(packet)["budget_count_scope"])
            # Counted as text (about three characters a token), not as envelope bytes.
            self.assertGreater(len(packet["text"]), 2 * packet["usage"]["output_tokens"])

    def test_matches_left_out_are_said_in_the_text_with_the_budget_that_includes_them(self):
        self.fill()
        packet = self.ask("export retry", 800)
        note = packet["omitted_for_budget"]
        self.assertGreater(note["omitted"], 0)
        self.assertEqual(note["matched"], note["shown"] + note["omitted"])
        self.assertIn(f"Showing {note['shown']} of {note['matched']} matches", packet["text"])
        self.assertIn("omitted for budget", packet["text"])
        again = self.ask("export retry", note["budget_to_include"])
        self.assertNotIn("omitted_for_budget", again)
        self.assertNotIn("omitted for budget", again["text"])
        self.assertEqual(note["matched"], len(again["sources"]))

    def test_nothing_matched_is_not_the_same_as_matched_but_did_not_fit(self):
        self.fill(3)
        none = self.ask("zzzzunmatchedterm", 600)
        self.assertIn("No relevant memory", none["text"])
        self.assertNotIn("omitted for budget", none["text"])
        packet = self.ask("export retry", 300)
        self.assertEqual("partial", packet["status"])
        self.assertIn("none of it fit the budget", packet["text"])
        self.assertNotIn("No relevant memory", packet["text"])

    def test_budget_insufficient_text_names_the_minimum(self):
        packet = self.ask("export retry", 20)
        self.assertEqual("budget_insufficient", packet["status"])
        minimum = packet["minimum_required_tokens"]
        self.assertGreater(minimum, 20)
        self.assertIn(f"at least {minimum} tokens", packet["text"])
        self.assertEqual("rendered_text", self.receipt(packet)["budget_count_scope"])
        self.assertEqual("budget_insufficient", self.ask("export retry", minimum - 1)["status"])
        self.assertNotEqual("budget_insufficient", self.ask("export retry", minimum)["status"])

    def test_the_advertised_minimum_budget_works_for_an_empty_and_a_small_briefing(self):
        empty = self.ask("export retry", 256)
        self.assertNotEqual("budget_insufficient", empty["status"])
        self.assertLessEqual(empty["usage"]["output_tokens"], 256)
        self.capture("one", "the export retry policy is documented here")
        small = self.ask("export retry", 256)
        self.assertNotEqual("budget_insufficient", small["status"])
        self.assertEqual(1, len(small["sources"]))
        self.assertLessEqual(small["usage"]["output_tokens"], 256)

    def test_text_items_carry_reference_authority_time_and_source_only(self):
        self.capture("one", "the export retry policy is documented here")
        text = self.ask("export retry", 2000)["text"]
        self.assertIn("observed_episode", text)
        self.assertIn("source test, observed", text)
        for dropped in ("actor_reported", "literal_overlap", "bytes 0-"):
            self.assertNotIn(dropped, text)
        # The structured form keeps the rest.
        self.assertIn("literal_overlap", json.dumps(self.brief("export retry")["guidance"]))

    def test_a_text_briefing_replays_byte_for_byte(self):
        self.fill(4)
        output = []
        command = {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": "brief",
            "args": {
                "project": "q",
                "query": "export retry",
                "bindings": [{}],
                "max_output_tokens": 700,
                "tokenizer": ESTIMATE,
                "format": "text",
            },
        }
        Retrieval(self.store).execute(self.owner, command).emit(output.append)
        receipt = json.loads(output[0])["receipt"]
        replay = []
        Retrieval(self.store).execute(
            self.owner,
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": "replay",
                "args": {"project": "q", "receipt": receipt},
            },
        ).emit(replay.append)
        self.assertEqual(output[0], replay[0])


class EstimateTests(unittest.TestCase):
    def test_the_estimate_is_three_ascii_characters_per_token_and_a_token_per_other_character(self):
        self.assertEqual(0, estimate(""))
        self.assertEqual(1, estimate("ab"))
        self.assertEqual(100, estimate("a" * 300))
        self.assertEqual(4, estimate("abcé中文"))  # 1 + 3 non-ASCII
        count, usage = counter(ESTIMATE)
        self.assertEqual("estimate", usage["counting"])
        self.assertEqual(ESTIMATE, usage["tokenizer"])
        self.assertEqual(2, count("abcdef"))

    def test_the_estimate_is_conservative_for_english_and_code(self):
        try:
            from memoryking.adapters.token_count import encoding

            exact = encoding()
        except Exception:
            self.skipTest("exact tokenizer not installed")
        prose = "The retry policy for the nightly export job backs off for thirty seconds. " * 20
        code = "def retry(self, attempt: int) -> float:\n    return min(2 ** attempt, 60.0)\n" * 20
        for sample in (prose, code):
            self.assertGreaterEqual(estimate(sample), len(exact.encode_ordinary(sample)))


class LocalBudgetTests(BudgetCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown


class PostgresBudgetTests(BudgetCases, pg.NativeFixture):
    pass


if __name__ == "__main__":
    unittest.main()


class ShortFirstPackingTests(unittest.TestCase):
    """`pack_rendered` with quotes that carry a short form (no store needed)."""

    @staticmethod
    def item(name, size, short=None, **extra):
        item = {"reference": name, "text": name * size, **extra}
        if short:
            item["_narrow"] = {"text": name * short}
        return item

    def run_pack(self, groups, budget, contiguous=False):
        from memoryking.domain.briefing import pack_rendered

        base = {"status": "partial", "guidance": [], "usage": {"output_tokens": 0}}

        def render(packet):
            return " ".join(i["text"] for i in packet["guidance"]) + (
                "!" if "omitted_for_budget" in packet else ""
            )

        count = len
        return pack_rendered(base, groups, count, budget, render, contiguous)

    def test_later_records_start_short_and_leftover_budget_widens_them_in_rank_order(self):
        groups = [[self.item(c, 400, 100)] for c in "abcd"]
        raw, used, selected, omitted = self.run_pack(groups, 1350)
        self.assertEqual([], omitted)
        self.assertEqual(
            {"a": 400, "b": 400, "c": 400, "d": 100},
            {i["reference"]: len(i["text"]) for i in selected},
        )
        self.assertLessEqual(used, 1350)
        self.assertFalse(any("_narrow" in i for i in selected))
        # the caller's items are not changed, so a second packing reads the same forms
        self.assertTrue(all("_narrow" in g[0] for g in groups))
        self.assertEqual(raw, self.run_pack(groups, 1350)[0])

    def test_a_neighbouring_turn_never_takes_the_room_a_ranked_record_could_widen_into(self):
        groups = [[self.item(c, 400, 100)] for c in "abcd"]
        near = self.item("n", 300, None, selection_reason="neighbouring_turn")
        near["_neighbour_of"] = "a"
        groups.append([near])
        _, _, selected, _ = self.run_pack(groups, 1650)
        sizes = {i["reference"]: len(i["text"]) for i in selected}
        self.assertEqual({"a": 400, "b": 400, "c": 400, "d": 400}, sizes)

    def test_nothing_changes_for_quotes_without_a_short_form(self):
        groups = [[self.item(c, 400)] for c in "abcd"]
        _, _, selected, omitted = self.run_pack(groups, 1000)
        self.assertEqual(["a", "b"], [i["reference"] for i in selected])
        self.assertIn("optional_group_budget", omitted)
