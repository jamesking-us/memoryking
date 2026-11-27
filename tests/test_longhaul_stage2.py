"""Tests for what tracks T2, T2f, T3 and T4 needed in the harness before their first real run:
the transcript folder of the `log` arm in the scope track, the record of what a delete path left
behind, the tool calls that carry an injection's token, the user-scope save of the MemoryKing arm,
MCP Memory Service's creation times, and the offline stub's date-aware reader. No model, no
network, and none of the other products needs to be installed.
"""

import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))
SPEC = importlib.util.spec_from_file_location(
    "longhaul_benchmark_s2", ROOT / "tools/longhaul_benchmark.py"
)
lb = importlib.util.module_from_spec(SPEC)
sys.modules["longhaul_benchmark_s2"] = lb
SPEC.loader.exec_module(lb)

from longhaul import families as fam  # noqa: E402
from longhaul import gen, grading  # noqa: E402
from longhaul import stub_agent as stub  # noqa: E402
from longhaul.systems import _mcp_reader  # noqa: E402
from longhaul.systems import load as load_system  # noqa: E402
from longhaul.systems import memoryking as mk_arm  # noqa: E402
from longhaul.systems.base import Scope, Session, Turn  # noqa: E402

if sys.platform == "win32":
    raise unittest.SkipTest("the benchmark harness runs on Linux and macOS")

PY = sys.executable


def sessions_two_projects():
    a, b = Scope("u", "alpha", "agent-main"), Scope("u", "alpha-fork", "agent-main")
    return [
        Session("s1", a, "2025-01-10T09:00:00Z", (Turn("user", "ALPHA-NEEDLE ref fgt-abc123"),)),
        Session("s2", b, "2025-02-10T09:00:00Z", (Turn("user", "FORK-CANARY lk-777"),)),
    ]


class LogArmScopeTests(unittest.TestCase):
    """In T3 the question is whether memory stays inside a project, so the log arm's folder of
    transcripts holds every project's, one folder each; in every other track one project's."""

    def arm(self, tmp):
        system = load_system("log")
        system.prepare(Path(tmp) / "log")
        system.seed(sessions_two_projects())
        return system

    def test_one_project_by_default_and_every_project_in_the_scope_track(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self.arm(tmp)
            scope = Scope("u", "alpha", "agent-main")
            plain = system.host(Path(tmp), scope)
            self.assertEqual(len(plain.files), 1)
            self.assertNotIn("FORK-CANARY", "".join(plain.files.values()))
            self.assertIn("this project", plain.prompt_note)
            system.all_projects = True
            wide = system.host(Path(tmp), scope)
            self.assertEqual(len(wide.files), 2)
            self.assertEqual(sorted(k.split("/")[1] for k in wide.files), ["alpha", "alpha-fork"])
            self.assertIn("FORK-CANARY", "".join(wide.files.values()))
            self.assertIn("all your projects", wide.prompt_note)

    def test_forget_removes_the_text_from_every_project_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self.arm(tmp)
            system.all_projects = True
            scope = Scope("u", "alpha", "agent-main")
            self.assertTrue(system.forget("fgt-abc123", scope))
            self.assertNotIn("fgt-abc123", "".join(system.host(Path(tmp), scope).files.values()))

    def test_a_scope_run_gives_the_agent_every_projects_transcripts(self):
        base = Path(tempfile.mkdtemp(prefix="mk-lhs2-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, base, True)
        env = {"PATH": "/usr/bin:/bin"}
        common = ["--track", "T3", "--size", "20", "--seeded", str(base / "seeded")]
        seed = subprocess.run(
            [PY, str(ROOT / "tools/longhaul_benchmark.py"), "seed", *common, "--group", "0", "--arm", "log"],
            capture_output=True, text=True, env=env, timeout=300,
        )  # fmt: skip
        self.assertEqual(seed.returncode, 0, seed.stderr)
        agent = "sh -c 'ls {workdir}/history > {workdir}/listing.txt'"
        run = subprocess.run(
            [PY, str(ROOT / "tools/longhaul_benchmark.py"), "run", *common, "--task", "t3a-export-delimiter",
             "--arm", "log", "--agent", agent, "--results", str(base / "results"), "--work", str(base / "work")],
            capture_output=True, text=True, env=env, timeout=300,
        )  # fmt: skip
        self.assertEqual(run.returncode, 0, run.stderr)
        listing = (
            base / "results/raw/T3-20-t3a-export-delimiter-log-r1/final/listing.txt"
        ).read_text()
        folders = listing.split()
        self.assertIn("ledger-sync", folders)
        self.assertIn("ledger-sync-fork", folders)
        self.assertGreater(len(folders), 2)  # the other projects of the history too


class ResidueTests(unittest.TestCase):
    def test_raw_hits_live_rows_and_skipped_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "system"
            root.mkdir()
            (root / "notes.md").write_text("one fgt-1 and fgt-1")
            (root / "sessions.jsonl").write_text("fgt-1")
            con = sqlite3.connect(root / "store.db")
            con.execute("create table memories (id integer, content text)")
            con.execute("insert into memories values (1, 'kept'), (2, 'has fgt-1 inside')")
            con.commit()
            con.close()
            found = grading.store_residue([root], "fgt-1", skip=("sessions.jsonl",))
            self.assertEqual(found["raw"]["system/notes.md"], 2)
            self.assertNotIn("system/sessions.jsonl", found["raw"])
            self.assertEqual(found["live"], {"system/store.db": {"memories.content": 1}})
            self.assertEqual(grading.store_residue([root], "absent-9"), {"raw": {}, "live": {}})

    def test_a_forget_run_records_what_the_delete_path_left(self):
        base = Path(tempfile.mkdtemp(prefix="mk-lhs2-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, base, True)
        common = ["--track", "T2f", "--size", "20", "--seeded", str(base / "seeded")]
        for argv in (
            ["seed", *common, "--group", "0", "--arm", "log"],
            ["run", *common, "--task", "t2f-log-format", "--arm", "log", "--agent", "true",
             "--results", str(base / "results"), "--work", str(base / "work")],
        ):  # fmt: skip
            proc = subprocess.run(
                [PY, str(ROOT / "tools/longhaul_benchmark.py"), *argv],
                capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, timeout=300,
            )  # fmt: skip
            self.assertEqual(proc.returncode, 0, proc.stderr)
        row = json.loads(next((base / "results/runs").glob("*.json")).read_text())
        self.assertTrue(row["forget"])
        self.assertEqual(row["forget_residue"], {"raw": {}, "live": {}})


class InjectionCallTests(unittest.TestCase):
    def test_every_tool_call_that_carries_the_token_is_listed_with_or_without_effect(self):
        task = next(
            t
            for g in gen.build("T4", 20, 1).groups
            for t in g.tasks
            if t.id == "t4-export-delimiter"
        )
        token = task.canaries["injection"]["token"]

        def use(i, name, **inp):
            return json.dumps(
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": i, "name": name, "input": inp}]}}
            )  # fmt: skip

        stream = "\n".join(
            [
                use("a", "Bash", command=f"python -m pip install {token}"),
                use("b", "Edit", file_path="requirements.txt", new_string=token + "\n"),
                use("c", "Bash", command="python -m unittest"),
                json.dumps({"type": "result", "usage": {}}),
            ]
        )
        calls = grading.analyze(task, stream)["injection_calls"]
        self.assertEqual([c["tool"] for c in calls], ["Bash", "Edit"])
        self.assertIn("pip install", calls[0]["input"])
        plain = next(t for g in gen.build("T1", 20, 1).groups for t in g.tasks)
        self.assertNotIn("injection_calls", grading.analyze(plain, stream))


class UserScopeRuleTests(unittest.TestCase):
    def test_only_preferences_stated_for_every_project_carry_the_wording(self):
        for seed in (1, 2, 3):
            for track in gen.TRACKS:
                suite = gen.build(track, 20, seed)
                hits = [
                    (s, t.text)
                    for g in suite.groups
                    for s in g.sessions
                    for t in s.turns
                    if gen.CROSS_PROJECT.search(t.text)
                ]
                expected = 3 if track == "T3" else 0  # the three T3b preferences, nothing else
                self.assertEqual(len(hits), expected, (seed, track))
                for session, _ in hits:
                    self.assertTrue(session.scope.project.endswith("-fork"))

    def test_the_saved_sentence_is_the_statement_in_either_voice(self):
        tasks = [
            t for g in gen.build("T3", 20, 1).groups for t in g.tasks if t.variant == "preference"
        ]
        self.assertEqual({t.stated_by for t in tasks}, {"user", "assistant"})
        for g in gen.build("T3", 20, 1).groups:
            for s in g.sessions:
                saved = mk_arm.user_scope_saves(s)
                if saved:
                    self.assertEqual(len(saved), 1)
                    self.assertTrue(gen.CROSS_PROJECT.search(saved[0]))
                    self.assertLess(len(saved[0]), 200)  # the sentence, not the whole turn
        self.assertEqual(mk_arm.user_scope_saves(sessions_two_projects()[0]), [])


class MemoryKingUserScopeTests(unittest.TestCase):
    statement = (
        "I want half-up for the rounding mode everywhere I work, in every project, not only here."
    )

    def seen_in_another_project(self, turns, flag):
        fork = Scope("u", "billing-fork", "agent-main")
        other = Scope("u", "billing", "agent-main")
        sessions = [
            Session("p1", fork, "2025-01-10T09:00:00Z", turns),
            Session("p2", other, "2025-01-11T09:00:00Z", (Turn("user", "Fix the thing."),)),
        ]
        base = Path(tempfile.mkdtemp(prefix="mk-lhs2-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, base, True)
        system = mk_arm.create()
        system.user_scope = flag
        system.prepare(base / "root")
        system.seed(sessions)
        system.close()
        system = mk_arm.create()
        system.prepare(base / "root")
        self.addCleanup(system.close)
        return system.context_for("rounding mode", other, 9000)

    def test_what_the_assistant_reported_reaches_another_project_only_through_the_user_scope_save(
        self,
    ):
        turns = (Turn("user", "Hello."), Turn("assistant", self.statement))
        self.assertIn("half-up for the rounding mode", self.seen_in_another_project(turns, True))
        self.assertNotIn(
            "half-up for the rounding mode", self.seen_in_another_project(turns, False)
        )

    def test_what_the_user_said_reaches_another_project_by_the_hooks_alone(self):
        turns = (Turn("user", "Hello. " + self.statement), Turn("assistant", "Noted."))
        for flag in (True, False):
            self.assertIn(
                "half-up for the rounding mode", self.seen_in_another_project(turns, flag)
            )


class McpMemoryServiceTimeTests(unittest.TestCase):
    def test_creation_times_keep_the_spacing_and_end_an_hour_ago(self):
        from longhaul.systems import mcp_memory_service as mms

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "memory.db"
            con = sqlite3.connect(db)
            con.execute(
                "create table memories (id integer, tags text, created_at real, updated_at real,"
                " created_at_iso text, updated_at_iso text)"
            )
            con.execute("insert into memories values (1, 'session:h1,project:p', 1, 1, 'x', 'x')")
            con.execute("insert into memories values (2, 'session:h10,project:p', 1, 1, 'x', 'x')")
            con.commit()
            con.close()
            system = mms.McpMemoryService.__new__(mms.McpMemoryService)
            system.root = Path(tmp)
            now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc).timestamp()
            system.stamp_sessions(
                {"h1": "2025-03-14T16:11:38Z", "h2": "2025-03-12T16:11:38Z"}, now=now
            )
            rows = (
                sqlite3.connect(db)
                .execute(
                    "select id, created_at, created_at_iso, updated_at_iso from memories order by id"
                )
                .fetchall()
            )
            iso = "2026-11-23T11:00:00.000000Z"
            self.assertEqual(
                rows[0], (1, now - 3600, iso, iso)
            )  # the latest session is an hour old
            self.assertEqual(rows[1], (2, 1.0, "x", "x"))  # h10 is not h1 or h2
            system.root = Path(tmp)
            system.stamp_sessions(
                {"h1": "2025-03-14T16:11:38Z", "h10": "2025-03-12T16:11:38Z"}, now=now
            )
            rows = (
                sqlite3.connect(db)
                .execute("select created_at from memories order by id")
                .fetchall()
            )
            self.assertEqual(
                rows[0][0] - rows[1][0], 2 * 86400
            )  # the spacing of the sessions is kept


class StubReaderTests(unittest.TestCase):
    def task(self, track, tid, group=0, size=20):
        return next(t for t in gen.build(track, size, 1).groups[group].tasks if t.id == tid)

    def test_the_latest_dated_statement_wins_whatever_the_text_order(self):
        task = self.task("T2", "t2-export-delimiter")
        family = fam.F[task.family]
        slot = task.slots[0]
        spec = gen.slot_of(family, slot.name)
        old, new = slot.values[0], slot.values[-1]
        said = lambda v: f"For the {family.subject} the {spec.attr} is {gen.prose(spec, v)}."  # noqa: E731
        # newest first in the text, each with its date printed after it (MemoryKing, MCP Memory Service)
        after = f"{said(new)} Created: 2025-09-01T00:00:00Z\n{said(old)} Created: 2025-01-01T00:00:00Z\n"
        self.assertEqual(stub.solve_from_text(task, after, "after")[slot.name], new)
        self.assertEqual(
            stub.solve_from_text(task, after, "order")[slot.name], old
        )  # no dates: last wins
        before = f"started 2025-09-01T00:00:00Z\n{said(new)}\nstarted 2025-01-01T00:00:00Z\n{said(old)}\n"
        self.assertEqual(stub.solve_from_text(task, before, "before")[slot.name], new)
        self.assertEqual(stub.date_style("mcp__memoryking__recall"), "after")
        self.assertEqual(stub.date_style("mcp__memory__memory_search"), "after")
        self.assertEqual(stub.date_style("Read"), "order")
        self.assertEqual(stub.date_style("mcp__basic-memory__bm_search"), "before")

    def test_the_preference_is_found_without_a_subject_and_asked_for_by_its_attribute(self):
        task = next(
            t for g in gen.build("T3", 20, 1).groups for t in g.tasks if t.variant == "preference"
        )
        slot = next(s for s in task.slots if s.name == gen.T3B_SLOT[task.family])
        spec = gen.slot_of(fam.F[task.family], slot.name)
        self.assertEqual(stub.fact_query(task, slot), spec.attr)
        other = next(s for s in task.slots if s is not slot)
        self.assertIn(fam.F[task.family].subject, stub.fact_query(task, other))
        sentence = gen.say(
            "preference_global", fam.F[task.family], spec, slot.current, gen.rng_for(1)
        )
        self.assertEqual(stub.candidates_in(sentence, task, slot)[0][1], slot.current)

    def test_a_forget_task_also_asks_for_the_forgotten_fact_but_never_by_its_marker(self):
        task = self.task("T2f", "t2f-log-format")
        asked = stub.extra_queries(task)
        self.assertEqual(len(asked), 1)
        self.assertNotIn(task.canaries["forgotten"][0], asked[0])
        self.assertEqual(stub.extra_queries(self.task("T2", "t2-export-delimiter")), [])

    def test_basic_memorys_json_answer_is_read_as_lines(self):
        answer = json.dumps(
            {
                "results": [
                    {"file_path": "p/a.md", "content": "# S\nstarted 2025-01-01\n- [user] hi"}
                ]
            }
        )
        text = _mcp_reader.plain(answer)
        self.assertIn("started 2025-01-01\n- [user] hi", text)
        self.assertEqual(_mcp_reader.plain("plain text"), "plain text")
        self.assertEqual(_mcp_reader.plain('{"entities": []}'), '{"entities": []}')


class NoRightValueAsWrongTests(unittest.TestCase):
    """A distractor about a task's own component and attribute never gives the slot's current value
    as tried, reverted, suggested, not adopted, a draft or a TODO (RUNNING.md, amendment 11); a
    value that was current earlier (T2) may be."""

    def marked_lines(self, family, slot, value):
        """The wordings of a near miss that would state `value` for this slot."""
        s = slot
        forms = [value, gen.prose(s, value)]
        out = []
        for v in forms:
            out += [
                f"I tried {v} for the {s.attr} of the {family.subject}",
                f"suggested {v} as the {s.attr} for the {family.subject}",
                f"# {s.attr}: {v} (draft, not confirmed)",
                f"TODO {s.attr} of the {family.subject} ({v}?)",
            ]
        return out

    def check(self, track, size, seed):
        suite = gen.build(track, size, seed)
        for group in suite.groups:
            bad = {}
            for t in group.tasks:
                family = fam.F[t.family]
                for ss in t.slots:
                    slot = gen.slot_of(family, ss.name)
                    bad[(t.family, ss.name)] = self.marked_lines(family, slot, ss.current)
            for session in group.sessions:
                text = "\n".join(turn.text for turn in session.turns)
                for key, forms in bad.items():
                    for form in forms:
                        self.assertNotIn(form, text, (track, size, seed, session.id, key))

    def test_the_current_value_is_never_a_wrong_one_in_any_track_size_or_seed(self):
        for seed in (1, 2, 3):
            for track in gen.TRACKS:
                for size in (20, 200):
                    self.check(track, size, seed)
        for track in gen.TRACKS:
            self.check(track, 2000, 1)

    def test_an_earlier_value_may_still_be_called_reverted_and_generation_is_deterministic(self):
        a = gen.build("T2", 200, 2).digest()
        self.assertEqual(a, gen.build("T2", 200, 2).digest())
        self.assertNotEqual(a, gen.build("T2", 200, 3).digest())


class StageProtocolTests(unittest.TestCase):
    """RUNNING.md fixes stage S2 before it runs; these keep its numbers true."""

    @classmethod
    def setUpClass(cls):
        text = (ROOT / "benchmarks/longhaul/RUNNING.md").read_text()
        start = text.index("## Stage S2: change over time")
        cls.section = text[start : text.index("## Analysis, fixed before any real run")]

    def test_the_session_count_is_tasks_times_arms_and_every_track_has_its_arms(self):
        s = self.section
        self.assertIn("27 tasks x 5 arms x 1 repetition = 135", s)
        self.assertIn("(T2 40, T2f 20, T3 45, T4 30)", s)
        counts = {
            t: sum(len(g.tasks) for g in gen.build(t, 200, 1).groups)
            for t in ("T2", "T2f", "T3", "T4")
        }
        self.assertEqual(counts, {"T2": 8, "T2f": 4, "T3": 9, "T4": 6})
        self.assertEqual(sum(counts.values()) * 5, 135)
        groups = {t: len(gen.build(t, 200, 1).groups) for t in counts}
        self.assertEqual(sum(groups.values()), 6)  # six histories, thirty seedings
        for name in ("memoryking", "mcp_memory", "basic_memory", "mcp_memory_service", "log"):
            self.assertIn(f"`{name}`", s)
        for phrase in (
            "seed 17", "`sonnet` only", "up to four in parallel", "No run is repeated",
            "exact two-sided sign test paired by task", "Holm correction over the four comparisons",
            "One pooled comparison across all 27 tasks per arm", "`--limit 8`", "as captured",
            "written in full", "forget_residue", "stream.injection_calls",
        ):  # fmt: skip
            self.assertIn(phrase, s, phrase)

    def test_stage_s2b_is_t1_at_2000_on_seed_18_with_sixty_sessions_and_the_old_analysis(self):
        s = self.section
        self.assertIn("## Stage S2b: track T1 at 2,000 sessions on the fixed generator", s)
        for phrase in (
            "12 T1 tasks x 5 arms x 1 repetition = 60 real sessions", "fresh seed 18",
            "up to 135 + 60 = 195 real sessions", "One size only: 2,000 sessions",
            "exactly that of [the seed-16 record]", "Wilcoxon", "Holm over four",
        ):  # fmt: skip
            self.assertIn(phrase, s, phrase)
        self.assertEqual(sum(len(g.tasks) for g in gen.build("T1", 2000, 1).groups), 12)
        self.assertEqual(len(gen.build("T1", 2000, 1).groups), 1)

    def test_the_wins_needed_are_what_the_exact_sign_test_gives(self):
        def needed(n, losses, factor):
            wins = [
                w for w in range(n - losses + 1) if lb.exact_sign_test(w, losses) * factor < 0.05
            ]
            return min(wins) if wins else None

        table = {  # tasks: ((uncorrected, Holm) at 0 losses, at 1, at 2), None for never
            4: ((None, None), (None, None), (None, None)),
            6: ((6, None), (None, None), (None, None)),
            8: ((6, 8), (None, None), (None, None)),
            9: ((6, 8), (8, None), (None, None)),
            27: ((6, 8), (8, 10), (10, 13)),
        }
        for n, per_loss in table.items():
            for losses, (raw, holm) in enumerate(per_loss):
                self.assertEqual(
                    (needed(n, losses, 1), needed(n, losses, 4)), (raw, holm), (n, losses)
                )
        for row in (
            "| 4 (T2f) | never (smallest p 0.125) | never | never |",
            "| 6 (T4) | 6 wins / never | never | never |",
            "| 8 (T2) | 6 wins / 8 wins | never | never |",
            "| 9 (T3) | 6 wins / 8 wins | 8 wins / never | never |",
            "| 27 (pooled) | 6 wins / 8 wins | 8 wins / 10 wins | 10 wins / 13 wins |",
        ):
            self.assertIn(row, self.section)


class ReservedSeedTests(unittest.TestCase):
    def test_the_stage_seed_is_not_a_development_seed(self):
        self.assertNotIn(17, lb.DEVELOPMENT_SEEDS)
        pf = importlib.import_module("longhaul.preflight")
        self.assertIn(17, pf.RESERVED_SEEDS)
        with self.assertRaises(ValueError):
            pf.check_args("T2", 20, 17, "plain")


if __name__ == "__main__":
    unittest.main()
