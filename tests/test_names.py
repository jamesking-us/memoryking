"""Records that hold the query's words side by side come first, and the list says so.

No store: packets are built by hand. The rule is mechanical (pairs of neighbouring query words
that are not function words, see `memoryking.domain.names`) and reads nothing of what a record
is about."""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from memoryking import hooks, host_hooks
from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
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

    def test_the_records_that_hold_the_words_together_are_found_and_come_first_for_a_cut(self):
        first, shared = names.split(ITEMS, QUERY)
        self.assertEqual([2, 3], first)
        self.assertTrue(any("gateway client settings" in p for p in shared))
        self.assertEqual(["gateway client settings"], names.held(ITEMS[2]["text"], shared)[:1])
        self.assertEqual([], names.held(ITEMS[0]["text"], ["gateway client settings"]))
        ordered = names.name_first(ITEMS, QUERY)
        self.assertEqual([3, 4], [int(i["reference"][4:], 16) for i in ordered[:2]])
        self.assertEqual([1, 2, 5], [int(i["reference"][4:], 16) for i in ordered[2:]])

    def test_a_short_list_or_one_where_nothing_tells_records_apart_is_left_alone(self):
        self.assertEqual(([], []), names.split(ITEMS[:3], QUERY))
        same = [item(f"gateway client settings default listen port {n}", n, n) for n in range(5)]
        self.assertEqual(([], []), names.split(same, QUERY))
        self.assertEqual(([], []), names.split(ITEMS, "the and of it"))
        self.assertEqual(same, names.name_first(same, QUERY))

    def test_the_rendered_list_stays_newest_first_and_marks_the_records_that_hold_the_words(self):
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
        # strictly newest first, so a later record is never listed before an earlier one
        order = [
            text.index(s)
            for s in ("worker settings", "export client", "webhook settings", "7411", "reverted")
        ]
        self.assertEqual(sorted(order), order)
        self.assertEqual(2, text.count("holds `gateway client settings`"))
        self.assertIn("2 are marked `holds`", text)
        self.assertIn("the other 3 hold fewer of them", text)
        self.assertIn("Newest first.", text)
        # a list with no such split keeps the older line and marks nothing
        plain = render_brief({**packet, "guidance": ITEMS[:3]})[0]
        self.assertIn("Newest first.", plain)
        self.assertNotIn("holds", plain)

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


class StoreTests(unittest.TestCase):
    """The same grouping, end to end on a real local service: `recall` and the prompt hook."""

    PROMPT = "Which default listen port did we settle for the gateway client settings?"

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-nm-")
        root = Path(cls.temp.name)
        owner = LocalStore.initialize(root / "db", root / "ctl").read_text().strip()
        cls.store = LocalStore(root / "db", root / "ctl")
        cls.server = Server(cls.store)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        token = cls.store.grant(owner, "agent", "alpha", ["capture", "read"])
        cls.credential = root / "agent.credential"
        cls.credential.write_text(token)
        cls.credential.chmod(0o600)
        rows = [
            "Confirmed: gateway client settings, default listen port: 7411.",
            "I tried 6044 for the default listen port of the gateway client settings; reverted.",
        ] + [
            f"Compliance says the default listen port of the {kind} settings has to be {n}."
            for n, kind in enumerate(["worker", "export", "webhook", "billing", "audit"], 9000)
        ]
        for n, text in enumerate(rows):
            hooks.capture(f"User: {text}", source="decision", event=f"e{n}", **cls.where())

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=5)
        cls.server.server_close()
        cls.store.close()
        cls.temp.cleanup()

    @classmethod
    def where(cls):
        return {"socket": cls.server.path, "credential": cls.credential, "project": "alpha"}

    def test_recall_marks_the_records_that_name_the_component_and_stays_newest_first(self):
        text = hooks.recall(self.PROMPT, text=True, **self.where())["text"]
        self.assertIn("are marked `holds`", text)
        body = text.split("## Sources")[0]
        marked = [ln for ln in body.splitlines() if "holds `gateway client settings`" in ln]
        self.assertEqual(2, len(marked))
        self.assertTrue(all("7411" in ln or "6044" in ln for ln in marked))
        # the newest record (about another component) is still first
        self.assertLess(body.index("audit settings"), body.index("7411"))

    def test_the_prompt_hook_marks_them_too(self):
        event = host_hooks.read_event(
            json.dumps(
                {"session_id": "s", "hook_event_name": "UserPromptSubmit", "prompt": self.PROMPT}
            ),
            {},
        )
        shown = host_hooks.prompt(event, floor=1, **self.where())
        self.assertIn("7411", shown)
        self.assertIn("holds `gateway client settings`", shown)


if __name__ == "__main__":
    unittest.main()
