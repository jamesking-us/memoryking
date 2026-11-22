"""The prompt-time hook in a crowded history, end to end on a real local service.

When far more records match a prompt than one lookup may return, the hook reads the next page of
the same ranked list, shows what fits its budget (chosen to differ from one another), says how
many matched in all, and says exactly how to read on or narrow."""

import json
import re
import tempfile
import threading
import unittest
from pathlib import Path

from memoryking import hooks, host_hooks, lean
from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore

PROMPT = "Implement write_orders in the orders export and follow what we settled for it."


def names(count):
    return [f"q{chr(97 + n // 26)}{chr(97 + n % 26)}x" for n in range(count)]


class CrowdedPromptTests(unittest.TestCase):
    COUNT = 90

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-pp-")
        cls.root = Path(cls.temp.name)
        owner = LocalStore.initialize(cls.root / "db", cls.root / "ctl").read_text().strip()
        cls.store = LocalStore(cls.root / "db", cls.root / "ctl")
        cls.server = Server(cls.store)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        token = cls.store.grant(owner, "agent", "alpha", ["capture", "read"])
        cls.credential = cls.root / "agent.credential"
        cls.credential.write_text(token)
        cls.credential.chmod(0o600)
        for n, name in enumerate(names(cls.COUNT)):
            hooks.capture(
                f"User: Settled for the orders export: the {name} is {name[::-1]}.",
                source="decision",
                event=f"e{n}",
                **cls.where(),
            )

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

    def submit(self, **options):
        event = host_hooks.read_event(
            json.dumps(
                {"session_id": "s", "hook_event_name": "UserPromptSubmit", "prompt": PROMPT}
            ),
            {},
        )
        return host_hooks.prompt(event, **self.where(), **options)

    def test_more_than_one_lookup_returns_is_read_as_two_pages_and_counted_exactly(self):
        shown = self.submit(budget=6000, tools="mcp")
        count = shown.count("**[g")
        self.assertGreater(count, host_hooks.PROMPT_LIMIT)  # the second page was read as well
        self.assertIn(f"found {self.COUNT} records", shown)
        self.assertIn(f"the closest {count} are below", shown)
        self.assertIn(f"{self.COUNT - count} more matched and are not shown", shown)
        self.assertNotIn("+ more", shown)
        # exactly where to read on: the records after the ones this list is made of
        self.assertIn(f"`offset` {host_hooks.PROMPT_LIMIT * host_hooks.PROMPT_PAGES}", shown)
        self.assertIn("`match` `all` requires every word", shown)
        self.assertIn("search again", shown)

    def test_the_next_page_the_hook_names_starts_after_everything_it_read(self):
        shown = self.submit(budget=6000)
        seen = set(re.findall(r"\b(q[a-z]{2}x)\b", shown))
        start = int(re.search(r"`offset` (\d+)", shown).group(1))
        rest = hooks.recall(
            "orders export settled",
            text=False,
            limit=host_hooks.PROMPT_LIMIT,
            offset=start,
            **self.where(),
        )
        later = set()
        for item in rest["guidance"]:
            later |= set(re.findall(r"\b(q[a-z]{2}x)\b", item["text"]))
        self.assertTrue(later)
        self.assertFalse(seen & later, "a record shown here is on the page the hook names")

    def test_a_small_budget_names_the_first_record_it_did_not_show(self):
        shown = self.submit(budget=800)
        count = shown.count("**[g")
        self.assertLess(count, host_hooks.PROMPT_LIMIT)
        self.assertIn(f"`offset` {count}", shown)

    def test_the_lean_tool_offers_offset_match_and_file_and_says_when_to_search_again(self):
        tool = next(t for t in lean.TOOLS if t["name"] == "recall")
        self.assertLessEqual({"offset", "match"}, set(tool["inputSchema"]["properties"]))
        self.assertNotIn("file", tool["inputSchema"]["properties"])  # `match: all` with the path
        self.assertEqual(["query"], tool["inputSchema"]["required"])
        self.assertIn("search again", tool["description"])


if __name__ == "__main__":
    unittest.main()
