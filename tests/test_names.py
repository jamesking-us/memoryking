"""Records that hold the query's words side by side come first, and the list says so.

No store: packets are built by hand. The rule is mechanical (pairs of neighbouring query words
that are not function words, see `memoryking.domain.names`) and reads nothing of what a record
is about."""

import unittest

from memoryking import host_hooks
from memoryking.domain import names
from memoryking.domain.briefing_text import render_brief

DAY = 86_400_000
BASE = 1_700_000_000_000
QUERY = "What is the default listen port of the gateway client settings?"


def item(text, n, day):
    return {
        "reference": f"obs_{n:032x}",
        "authority": "observed_episode",
        "text": text,
        "source": "claude-session",
        "actor": "agent",
        "observed_at_ms": BASE + day * DAY,
        "selection_reason": "literal_overlap",
    }


ITEMS = [
    # newest first would put the worker settings record in front
    item("Compliance says the default listen port of the worker settings is 9532.", 1, 9),
    item("A colleague suggested 6044 as the default listen port for the export client.", 2, 8),
    item("Confirmed: gateway client settings, default listen port: 7411.", 3, 3),
    item(
        "I tried 6044 for the default listen port of the gateway client settings; reverted.", 4, 2
    ),
    item("The listen port of the webhook settings is 8317, a different default.", 5, 7),
]


class NamesTests(unittest.TestCase):
    def test_a_pair_every_record_holds_weighs_nothing(self):
        texts = [str(i["text"]) for i in ITEMS]
        scores, pairs = names.weigh(QUERY, texts)
        self.assertIn(("gateway", "client"), pairs)
        # `default listen` and `listen port` are in four of five records: they weigh less than
        # the pair only two records hold
        self.assertGreater(scores[2], scores[0])
        self.assertGreater(scores[3], scores[0])
        every = ["default listen port gateway client"] * 3
        self.assertEqual([0.0, 0.0, 0.0], names.weigh(QUERY, every)[0])

    def test_the_records_that_hold_the_words_together_come_first_each_group_in_its_order(self):
        ordered, first, shared = names.name_first(ITEMS, QUERY)
        self.assertEqual(2, first)
        self.assertEqual([3, 4], [int(i["reference"][4:], 16) for i in ordered[:2]])
        self.assertEqual([1, 2, 5], [int(i["reference"][4:], 16) for i in ordered[2:]])
        self.assertTrue(any("gateway client settings" in p for p in shared))

    def test_a_short_list_or_one_where_nothing_tells_records_apart_is_left_alone(self):
        self.assertEqual((ITEMS[:3], 0, []), names.name_first(ITEMS[:3], QUERY))
        same = [item(f"gateway client settings default listen port {n}", n, n) for n in range(5)]
        self.assertEqual((same, 0, []), names.name_first(same, QUERY))
        self.assertEqual((ITEMS, 0, []), names.name_first(ITEMS, "the and of it"))

    def test_the_rendered_list_puts_them_first_newest_first_within_each_group_and_says_so(self):
        packet = {
            "version": 1,
            "status": "partial",
            "receipt": "rcpt_1",
            "objective": QUERY,
            "usage": {"max_output_tokens": 6000},
            "warnings": [],
            "constraints": [],
            "conflicts": [],
            "guidance": ITEMS,
        }
        text = render_brief(packet)[0]
        order = [
            text.index(s) for s in ("7411", "reverted", "worker settings", "export client", "8317")
        ]
        self.assertEqual(sorted(order), order[:2] + sorted(order[2:]))
        self.assertLess(order[0], order[1])  # 7411 (day 3) before reverted (day 2)
        self.assertIn("The first 2 hold the words you asked for side by side", text)
        self.assertIn("the other 3 hold fewer of them", text)
        self.assertIn("gateway client settings", text.split("Each group")[0])
        # a list with no such split keeps the older line
        plain = render_brief({**packet, "guidance": ITEMS[:3]})[0]
        self.assertIn("Newest first.", plain)
        self.assertNotIn("side by side", plain)

    def test_the_prompt_hook_shows_them_first_when_the_budget_cuts_the_list(self):
        many = [
            item(
                f"Settled: the default listen port of the export client settings is {n}.", 10 + n, n
            )
            for n in range(40)
        ]
        needed = item("Confirmed: gateway client settings, default listen port: 7411.", 99, 1)
        packet = {
            "version": 1,
            "status": "partial",
            "receipt": "rcpt_1",
            "objective": QUERY,
            "usage": {"max_output_tokens": 6000},
            "warnings": [],
            "constraints": [],
            "conflicts": [],
            "guidance": [*many, needed],
        }
        shown = host_hooks.prompt_view(packet, QUERY, 300, 32, None)
        self.assertIn("7411", shown)


if __name__ == "__main__":
    unittest.main()
