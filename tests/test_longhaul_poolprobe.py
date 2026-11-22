"""The pool diagnostic: where a needle ranks in the candidate pool and what outranks it.

No agent and no model. One end-to-end case seeds a 20-session history through the hook path and
reads the ranked list for every needle; the rest pin what the records above a needle are called.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))

from longhaul import gen, poolprobe  # noqa: E402
from longhaul.systems import load as load_system  # noqa: E402


class KindTests(unittest.TestCase):
    def test_a_record_is_named_by_the_component_and_attribute_it_states(self):
        kind = poolprobe.kind_of
        subject, attribute = "orders export", "column delimiter"
        self.assertEqual(
            "A",
            kind("User: The column delimiter of the orders export is a pipe.", subject, attribute),
        )
        self.assertEqual(
            "B", kind("User: The file encoding of the orders export is utf-8.", subject, attribute)
        )
        self.assertEqual(
            "C",
            kind("User: The column delimiter of the report pages is a tab.", subject, attribute),
        )
        self.assertEqual("D", kind("User: Can you take a look at the cache?", subject, attribute))
        # one record with sentences of two kinds is called by the closest of them
        both = "User: The file encoding of the orders export is utf-8. The column delimiter of the report pages is a tab."
        self.assertEqual("B", kind(both, subject, attribute))

    def test_the_summary_counts_what_the_pool_held(self):
        rows = [
            {"query": q, "in_pool": ok, "rank": rank, "scored": scored, "shown": shown, "above": {}}
            for q, ok, rank, scored, shown in (
                ("hook", True, 3, 256, True),
                ("hook", True, 9, 120, False),
                ("hook", False, None, 256, False),
            )
        ]
        summary = poolprobe.summarize(rows)
        self.assertEqual(
            {"needles": 3, "shown": 1, "not_in_pool": 1, "pool_full": 2, "median_rank": 9},
            summary["hook"],
        )
        self.assertIn("hook", poolprobe.render(summary))


class EndToEndTests(unittest.TestCase):
    def test_every_needle_of_a_small_history_is_in_the_pool_and_the_wrapper_is_removed(self):
        from memoryking.adapters import retrieval

        before = retrieval.Retrieval._present
        suite = gen.build("T1", 20, 1)
        group = suite.groups[0]
        root = Path(tempfile.mkdtemp(prefix="mk-pp-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, root, True)
        seeded = root / "s"
        system = load_system("memoryking")
        system.prepare(seeded)
        try:
            system.seed(group.sessions)
        finally:
            system.close()
        rows = poolprobe.probe_group(group, seeded, root / "w")
        self.assertEqual(before, retrieval.Retrieval._present)
        needles = sum(len([s for s in t.slots if s.required]) for t in group.tasks)
        self.assertEqual(needles * len(poolprobe.QUERIES), len(rows))
        for row in rows:
            self.assertTrue(row["in_pool"], row)
            self.assertGreaterEqual(row["scored"], row["rank"])
        self.assertTrue(all(r["shown"] for r in rows if r["query"] == "hook"))


if __name__ == "__main__":
    unittest.main()
