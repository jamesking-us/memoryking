"""The offline hook measure: which needle sentences the hooks and `recall` put in front of an agent.

No agent and no model. The needle sentences are found from the generated history, the careful
query is built from the prompt alone, and one end-to-end case seeds a 20-session history through
the hook path, then runs the installed hook commands and `recall` against it.
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))
sys.path.insert(0, str(ROOT / "tools"))

import longhaul_benchmark as lb  # noqa: E402
from longhaul import gen, hookprobe  # noqa: E402
from longhaul.systems import load as load_system  # noqa: E402


class NeedleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suite = gen.build("T1", 20, 1)

    def test_every_required_slot_of_every_task_has_a_needle_sentence_in_a_needle_session(self):
        for group in self.suite.groups:
            sessions = {s.id: s for s in group.sessions}
            for task in group.tasks:
                found = hookprobe.needle_sentences(task, sessions)
                self.assertEqual(len([s for s in task.slots if s.required]), len(found))
                for needle in found:
                    self.assertTrue(needle["sentences"], (task.id, needle["slot"]))
                    for sentence in needle["sentences"]:
                        self.assertTrue(
                            any(
                                sentence in turn.text
                                for sid in task.needle_sessions
                                for turn in sessions[sid].turns
                            ),
                            sentence,
                        )

    def test_a_needle_sentence_states_the_current_value_and_never_a_draft_or_reverted_one(self):
        group = self.suite.groups[0]
        sessions = {s.id: s for s in group.sessions}
        for task in group.tasks:
            for needle in hookprobe.needle_sentences(task, sessions):
                for sentence in needle["sentences"]:
                    for word in ("tried", "did not adopt", "TODO", "draft"):
                        self.assertNotIn(word, sentence)

    def test_matching_ignores_markdown_and_escaping_but_not_words(self):
        needles = [{"slot": "a", "sentences": ["We settled on a semicolon (;) as the delimiter."]}]
        shown = "1. **[g1]** We settled on a semicolon (;) as the *delimiter*. _(x)_"
        self.assertEqual([True], hookprobe.carried(shown, needles))
        self.assertEqual(
            [False], hookprobe.carried("We settled on a comma as the delimiter.", needles)
        )

    def test_the_careful_query_comes_from_the_prompt_alone(self):
        prompt = (
            "In ledger-sync, implement `write_orders(rows, path)` in `ledger/exporter.py`. "
            "Follow the conventions this team has already settled for the orders export. "
            "Standard library only."
        )
        self.assertEqual("the orders export write orders", hookprobe.careful_query(prompt))
        for group in self.suite.groups:
            for task in group.tasks:
                query = hookprobe.careful_query(task.prompt)
                self.assertTrue(query)
                self.assertEqual(query, hookprobe.careful_query(task.prompt))
                for slot in task.slots:  # blind to the history: no needle value in it
                    self.assertNotIn(slot.current.lower(), query.lower().split())


class TwoStepReaderTests(unittest.TestCase):
    """Step two's query: the prompt and the file the task names, never the history."""

    PROMPT = (
        "In ledger-sync, implement `write_orders(rows, path)` in `ledger/exporter.py`. "
        "Follow the conventions this team has already settled for the orders export."
    )
    FILE = (
        "# TODO: the audit trail\n"
        "class OrderWriter:\n"
        '    """Write the orders export file, one line per row."""\n'
        "    def write_orders(self, rows, path):\n"
        "        raise NotImplementedError\n"
    )

    def test_the_file_a_task_names_is_the_first_backticked_path_the_repository_holds(self):
        repo = {"ledger/exporter.py": self.FILE, "README.md": "x"}
        self.assertEqual("ledger/exporter.py", hookprobe.named_file(self.PROMPT, repo))
        self.assertIsNone(hookprobe.named_file(self.PROMPT, {"README.md": "x"}))
        self.assertIsNone(hookprobe.named_file("Nothing named here.", repo))

    def test_the_words_of_a_file_are_its_identifiers_comments_and_docstrings_once_each(self):
        words = hookprobe.file_words(self.FILE, {"orders"})
        self.assertEqual(
            ["todo", "the", "audit", "trail", "order", "writer", "write", "export", "file"],
            words[:9],
        )
        self.assertNotIn("orders", words)  # already in the query
        self.assertNotIn("self", words)
        self.assertNotIn("notimplementederror", words)
        self.assertEqual(len(words), len(set(words)))

    def test_the_query_is_the_careful_query_plus_at_most_sixteen_words_of_the_file(self):
        query = hookprobe.two_step_query(self.PROMPT, self.FILE)
        careful = hookprobe.careful_query(self.PROMPT)
        self.assertTrue(query.startswith(careful + " "))
        self.assertIn("audit", query.split())
        extra = query[len(careful) :].split()
        self.assertLessEqual(len(extra), hookprobe.FILE_WORDS)
        self.assertEqual(careful, hookprobe.two_step_query(self.PROMPT, ""))

    def test_the_rule_never_reads_a_needle_or_an_answer(self):
        suite = gen.build("T1", 20, 1)
        for group in suite.groups:
            for task in group.tasks:
                path = hookprobe.named_file(task.prompt, task.repo)
                self.assertTrue(path, task.id)
                query = hookprobe.two_step_query(task.prompt, task.repo[path])
                self.assertEqual(query, hookprobe.two_step_query(task.prompt, task.repo[path]))
                for slot in task.slots:
                    self.assertNotIn(slot.current.lower(), query.lower().split())
                # the file is the stub the agent is given: no oracle, no history
                self.assertNotIn(task.oracle[path], query)


class SummaryTests(unittest.TestCase):
    def test_totals_count_needles_and_tasks_that_are_complete(self):
        def row(carried):
            sources = hookprobe.SOURCES
            return {
                "task": "t",
                "needles": 2,
                "carried": {k: carried for k in (*sources, *hookprobe.STEPS)},
                "chars": {k: 100 for k in sources},
                "seconds": {k: 0.5 for k in sources},
            }

        summary = hookprobe.summarize([row([True, True]), row([True, False])])
        self.assertEqual((2, 4), (summary["tasks"], summary["needles"]))
        self.assertEqual(3, summary["hook"]["needles"])
        self.assertEqual(1, summary["hook"]["tasks_complete"])
        self.assertEqual(100, summary["start"]["chars"])
        self.assertIn("recall_careful", hookprobe.render(summary))
        self.assertEqual(3, summary["step_two"]["needles"])


class EndToEndTests(unittest.TestCase):
    def test_the_installed_hook_and_recall_are_measured_on_a_seeded_history(self):
        suite = gen.build("T1", 20, 1)
        group = suite.groups[0]
        root = Path(tempfile.mkdtemp(prefix="mk-hp-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, root, True)
        seeded = root / "s"
        system = load_system("memoryking")
        system.prepare(seeded)
        try:
            system.seed(group.sessions)
        finally:
            system.close()
        rows = hookprobe.probe_group(suite, group, seeded, root / "w", keep_text=True)
        self.assertEqual([t.id for t in group.tasks], [r["task"] for r in rows])
        for row in rows:
            self.assertEqual([], row["unmatched"])
            # at 20 sessions one recall for the prompt carries every needle
            self.assertEqual([True] * row["needles"], row["carried"]["recall_prompt"], row["task"])
            for key in hookprobe.SOURCES:
                self.assertEqual(row["chars"][key], len(row["text"][key]))
                self.assertGreaterEqual(row["seconds"][key], 0)
            # the prompt hook is bounded by its budget, and says what it shows
            self.assertLessEqual(row["chars"]["hook"] / 3, 2000 + 1, row["task"])
        self.assertGreater(sum(sum(r["carried"]["hook"]) for r in rows), 0)
        json.dumps(rows)  # a result record is plain data
        summary = hookprobe.summarize(rows)
        self.assertEqual(sum(r["needles"] for r in rows), summary["needles"])

    def test_the_subcommand_refuses_a_history_that_was_not_seeded(self):
        with self.assertRaises(lb.BenchmarkError):
            lb.cmd_hookprobe(
                type(
                    "Args",
                    (),
                    {
                        "track": "T1",
                        "size": 20,
                        "seed": 1,
                        "seeded": "/tmp/mk-hp-nothing",
                        "work": "/tmp/mk-hp-work",
                        "json": True,
                        "text": False,
                        "prompt_args": "",
                    },
                )()
            )


if __name__ == "__main__":
    unittest.main()
