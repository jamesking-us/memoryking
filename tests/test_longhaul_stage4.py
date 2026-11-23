"""Tests for what the lived track (T2b) and track T5 needed in the harness before their first real run:
a session's leftovers (the host's session-end hook) are waited for, every session start is in a durable
ledger before it happens, a usage-limit refusal is given back and not lost, the lived run keeps its raw
streams, T5 never makes a call twice, and the haystack is fed whole and with its own dates. No model, no
network; none of the other products needs to be installed.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))
SPEC = importlib.util.spec_from_file_location(
    "longhaul_benchmark_s4", ROOT / "tools/longhaul_benchmark.py"
)
lb = importlib.util.module_from_spec(SPEC)
sys.modules["longhaul_benchmark_s4"] = lb
SPEC.loader.exec_module(lb)

from longhaul import gen  # noqa: E402
from longhaul import stub_agent as stub  # noqa: E402
from longhaul.systems import basic_memory as bm  # noqa: E402
from longhaul.systems.base import Scope, Session, Turn  # noqa: E402

if sys.platform == "win32":
    raise unittest.SkipTest("the benchmark harness runs on Linux and macOS")

PY = sys.executable
STUB = ROOT / "benchmarks/longhaul/stub_agent.py"
FAKE = ROOT / "benchmarks/longhaul/fake_model.py"


def run_cli(*args):
    return subprocess.run(
        [PY, str(ROOT / "tools/longhaul_benchmark.py"), *map(str, args)],
        capture_output=True,
        text=True,
        timeout=600,
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
    )


class WaitForLeftoversTests(unittest.TestCase):
    def test_a_process_left_in_the_agents_session_is_waited_for_by_pid_only(self):
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-lhs4-") as tmp:
            marker = Path(tmp) / "late"
            proc = subprocess.Popen(
                ["sh", "-c", f"(sleep 1; echo done > {marker}) >/dev/null 2>&1 & exit 0"],
                start_new_session=True,
            )
            proc.wait()
            self.assertFalse(marker.exists())
            waited = lb.wait_session_quiet(proc.pid, 10)
            self.assertTrue(marker.exists())
            self.assertGreaterEqual(waited["settle_seconds"], 0.5)
            self.assertEqual(waited["left_running"], [])
            # nothing left: it returns at once; a timeout reports what is still running
            self.assertLess(lb.wait_session_quiet(proc.pid, 10)["settle_seconds"], 1)
            slow = subprocess.Popen(
                ["sh", "-c", "(sleep 3) >/dev/null 2>&1 & exit 0"], start_new_session=True
            )
            slow.wait()
            short = lb.wait_session_quiet(slow.pid, 0.3)
            self.assertTrue(short["left_running"])
            lb.wait_session_quiet(slow.pid, 10)

    def test_the_ledger_line_is_written_before_the_session_and_synced(self):
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-lhs4-") as tmp:
            ledger = Path(tmp) / "l.jsonl"
            lb.ledger_note(str(ledger), ev="session_start", run="r", n=1)
            lb.ledger_note(str(ledger), ev="session_end", run="r", n=1)
            lb.ledger_note(None, ev="ignored")
            rows = [json.loads(x) for x in ledger.read_text().splitlines()]
            self.assertEqual([r["ev"] for r in rows], ["session_start", "session_end"])
            self.assertIn("t", rows[0])


class LivedRunTests(unittest.TestCase):
    def lived(self, base, arm, agent, *extra, task="t2b-export-delimiter"):
        return run_cli(
            "lived", "--task", task, "--arm", arm, "--agent", agent, "--keep",
            "--ledger", base / "ledger.jsonl", "--results", base / "res", "--work", base / "work",
            *extra,
        )  # fmt: skip

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(dir="/tmp", prefix="mk-lhs4-"))
        self.addCleanup(shutil.rmtree, self.base, True)

    def result(self):
        return json.loads(next((self.base / "res/runs").glob("T2b-*.json")).read_text())

    def test_a_session_end_hook_that_outlives_the_cli_is_given_time_and_the_ledger_is_complete(
        self,
    ):
        agent = "sh -c '(sleep 1; echo x >> {workdir}/late.txt) >/dev/null 2>&1 & exit 0'"
        proc = self.lived(self.base, "off", agent, "--settle", 20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = self.result()
        self.assertEqual(len(result["sessions"]), 4)
        for s in result["sessions"]:
            self.assertGreaterEqual(s["settle_seconds"], 0.5)
            self.assertEqual(s["left_running"], [])
        raw = self.base / "res/raw" / result["task"].join(["T2b-0-", "-off-r1"])
        for n in range(1, 5):
            self.assertTrue((raw / f"agent{n}.stdout").exists())
        rows = [json.loads(x) for x in (self.base / "ledger.jsonl").read_text().splitlines()]
        self.assertEqual(
            [(r["ev"], r["n"]) for r in rows],
            [(e, n) for n in range(1, 5) for e in ("session_start", "session_end")],
        )

    def test_without_time_to_settle_the_hook_is_left_behind(self):
        agent = "sh -c '(sleep 2; echo x >> {workdir}/late.txt) >/dev/null 2>&1 & exit 0'"
        proc = self.lived(self.base, "off", agent, "--settle", 0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(
            all(s["left_running"] or s["settle_seconds"] < 0.5 for s in self.result()["sessions"])
        )

    def test_a_usage_limit_refusal_is_given_back_and_the_session_started_again(self):
        script = self.base / "agent.sh"
        script.write_text(
            "#!/bin/sh\n"
            f'if [ ! -f "$2.once" ]; then touch "$2.once";\n'
            '  echo \'{"type":"result","is_error":true,"result":"You have hit your usage limit"}\'; exit 0; fi\n'
            f'exec {PY} {STUB} "$1" "$2" --mode oracle\n'
        )
        script.chmod(0o755)
        proc = self.lived(
            self.base, "off", f"{script} {{workdir}} {{prompt_file}}", "--limit-wait", 1
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = [json.loads(x) for x in (self.base / "ledger.jsonl").read_text().splitlines()]
        events = [r["ev"] for r in rows]
        self.assertEqual(events.count("session_returned"), 1)
        self.assertEqual(events.count("session_start"), 5)  # four sessions, one started again
        self.assertEqual(events.count("session_end"), 4)
        self.assertTrue(self.result()["passed"])

    def test_the_refusal_check_reads_only_an_error_result(self):
        err = '{"type":"result","is_error":true,"result":"usage limit reached"}'
        self.assertTrue(lb.refused_for_limit(err))
        self.assertFalse(lb.refused_for_limit('{"type":"result","is_error":false,"result":"ok"}'))
        self.assertFalse(lb.refused_for_limit("no result"))

    def test_memoryking_session_end_hook_really_fires_and_every_arm_finds_the_correction(self):
        for arm in ("notes", "memoryking"):
            agent = f"{PY} {STUB} {{workdir}} {{prompt_file}} --mode lived --mcp-config {{mcp_config}} --settings {{settings}}"
            for old in (self.base / "res").glob("*"):
                shutil.rmtree(old, ignore_errors=True)
            shutil.rmtree(self.base / "work", ignore_errors=True)
            proc = self.lived(self.base, arm, agent, "--seed", 1)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = self.result()
            self.assertTrue(result["passed"], (arm, result["check"]))
            if arm == "memoryking":
                seen = [s["memory_after"]["observations"] for s in result["sessions"]]
                self.assertEqual(
                    seen, [2, 4, 6, 8]
                )  # the hook wrote after each session, and was waited for
                self.assertIn(
                    "SessionEnd",
                    json.loads(
                        (
                            self.base
                            / "work/lived-t2b-export-delimiter-memoryking-r1/bench/settings.json"
                        ).read_text()
                    )["hooks"],
                )


class LivedStubTests(unittest.TestCase):
    def test_the_correction_outranks_the_value_it_corrects_on_one_day(self):
        task = lb.lived_tasks(1)[0]["task"]
        family = stub.fam.F[task.family]
        slot = task.slots[0]
        spec = gen.slot_of(family, slot.name)
        old, new = slot.values[0], slot.values[1]
        first = f"Final answer from the team on the {family.subject}: the {spec.attr} is {gen.prose(spec, old)}.\n"
        fix = f"Scratch what I told you about the {family.subject}: the {spec.attr} is now {gen.prose(spec, new)} instead of {gen.prose(spec, old)}.\n"
        for text in (first + fix, fix + first):
            self.assertEqual(stub.solve_from_text(task, text, "chain")[slot.name], new)

    def test_the_prompts_name_the_fact_in_session_one_and_its_correction_in_session_three(self):
        for item in lb.lived_tasks(3):
            self.assertEqual(len(stub.statements_in(item["task"], item["prompts"][0])) >= 1, True)
            self.assertEqual(stub.statements_in(item["task"], item["prompts"][3]), [])


class T5Tests(unittest.TestCase):
    def fake_data(self):
        data = []
        for t in ("single-session-user", "multi-session", "knowledge-update"):
            for i in range(2):
                qid = f"{t}-{i}"
                data.append(
                    {
                        "question_id": qid,
                        "question_type": t,
                        "question": f"What is the code number for {t[:3]}{i}?",
                        "answer": f"{100 + i}",
                        "question_date": "2023/05/30 (Tue) 10:00",
                        "answer_session_ids": [f"{qid}-s1"],
                        "haystack_session_ids": [f"{qid}-s0", f"{qid}-s1"],
                        "haystack_dates": ["2023/05/01 (Mon) 09:00", "2023/05/02 (Tue) 09:00"],
                        "haystack_sessions": [
                            [{"role": "user", "content": "Hello there, nothing relevant."}],
                            [
                                {
                                    "role": "user",
                                    "content": f"The code number for {t[:3]}{i} is {100 + i} for sure.",
                                    "has_answer": True,
                                }
                            ],
                        ],
                    }
                )
        return data

    def t5(self, base, arm, reader, judge, ledger=True):
        data = base / "data.json"
        if not data.exists():
            data.write_text(json.dumps(self.fake_data()))
        return run_cli(
            "t5-run", "--arm", arm, "--data", data, "--size", 6, "--reader-command", reader,
            "--judge-command", judge, "--results", base / "res", "--work", base / "w",
            "--budget-chars", 4000, *(["--ledger", base / "ledger.jsonl"] if ledger else []),
        )  # fmt: skip

    def events(self, base):
        return [json.loads(x)["ev"] for x in (base / "ledger.jsonl").read_text().splitlines()]

    def test_no_call_is_made_twice_when_the_run_is_started_again(self):
        base = Path(tempfile.mkdtemp(dir="/tmp", prefix="mk-lhs4-"))
        self.addCleanup(shutil.rmtree, base, True)
        reader, judge = f"{PY} {FAKE} reader", f"{PY} {FAKE} judge"
        self.assertEqual(self.t5(base, "log", reader, judge).returncode, 0)
        self.assertEqual(self.events(base).count("reader_start"), 6)
        self.assertEqual(self.events(base).count("judge_start"), 6)
        (
            base / "res/t5/log-6.json"
        ).unlink()  # the run is started again: every question has its file
        failing = "false"
        proc = self.t5(base, "log", failing, failing)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.events(base).count("reader_start"), 6)  # none made again
        self.assertTrue((base / "res/t5/log-6.json").exists())

    def test_a_call_that_was_announced_and_never_answered_is_lost_and_not_asked_again(self):
        base = Path(tempfile.mkdtemp(dir="/tmp", prefix="mk-lhs4-"))
        self.addCleanup(shutil.rmtree, base, True)
        data = base / "data.json"
        data.write_text(json.dumps(self.fake_data()))
        qs = __import__("longhaul.lme", fromlist=["x"]).select(self.fake_data(), 6, 1)
        import hashlib

        first = qs[0]["question_id"]
        qid = (
            "".join(c if c.isalnum() else "-" for c in first)[:30]
            + "-"
            + hashlib.sha1(first.encode()).hexdigest()[:8]
        )
        lb.ledger_note(
            str(base / "ledger.jsonl"), ev="reader_start", arm="log", qid=qid
        )  # then the machine died
        proc = self.t5(base, "log", f"{PY} {FAKE} reader", f"{PY} {FAKE} judge")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads((base / "res/t5/log-6.json").read_text())
        lost = [r for r in result["rows"] if r["lost_call"]]
        self.assertEqual([r["id"] for r in lost], [first])
        self.assertFalse(lost[0]["correct"])
        self.assertEqual(
            self.events(base).count("reader_start"), 6
        )  # the lost one and five more, not seven

    def test_the_reader_runs_in_a_neutral_directory_with_a_scrubbed_environment(self):
        base = Path(tempfile.mkdtemp(dir="/tmp", prefix="mk-lhs4-"))
        self.addCleanup(shutil.rmtree, base, True)
        probe = base / "probe.py"
        probe.write_text(
            "import os, sys\nsys.stdin.read()\nopen(sys.argv[1], 'a').write(os.getcwd() + '|' + os.environ.get('HOME', '') + '|' + str(sorted(k for k in os.environ if k.startswith('MK_SECRET'))) + '\\n')\nprint('I do not know')\n"
        )
        env = dict(os.environ, MK_SECRET_X="1")
        data = base / "data.json"
        data.write_text(json.dumps(self.fake_data()))
        proc = subprocess.run(
            [PY, str(ROOT / "tools/longhaul_benchmark.py"), "t5-run", "--arm", "log", "--data", str(data), "--size", "6",
             "--reader-command", f"{PY} {probe} {base / 'out.txt'}", "--judge-command", f"{PY} {FAKE} judge", "--results", str(base / "res"),
             "--work", str(base / "w"), "--budget-chars", "4000"],
            capture_output=True, text=True, env={**env, "PYTHONPATH": ""}, timeout=300,
        )  # fmt: skip
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = (base / "out.txt").read_text().splitlines()[0]
        cwd, home, secrets = line.split("|")
        self.assertIn("mk-lht5-", cwd)
        self.assertIn("mk-lht5-", home)  # a fresh, empty HOME
        self.assertEqual(secrets, "[]")  # the operator's environment does not reach the reader

    def test_the_haystack_is_fed_whole_and_a_long_turn_keeps_its_end(self):
        long_turn = "start " + "x" * 3000 + " THE-END"
        session = Session(
            "s", Scope("u", "p", "a"), "2023-05-01T09:00:00Z", (Turn("assistant", long_turn),)
        )
        self.assertNotIn(
            "THE-END", bm.BasicMemory.render(session)
        )  # the seeding rule of the other tracks clips
        self.assertIn("THE-END", bm.BasicMemory.render(session, None))  # track T5 does not


class McpMemoryServiceDuplicateTests(unittest.TestCase):
    def test_a_haystack_that_repeats_a_passage_is_stored_and_the_refusal_is_counted(self):
        from longhaul.systems import mcp_memory_service as mms

        class Client:
            def __init__(self):
                self.seen = set()

            def call(self, name, args):
                key = json.dumps(args["turns"])
                if key in self.seen:
                    return (
                        "Error storing session chunk 1/1: Duplicate content detected (exact match)",
                        None,
                        True,
                    )
                self.seen.add(key)
                return "Session stored successfully", None, False

        system = mms.McpMemoryService.__new__(mms.McpMemoryService)
        system.root = Path("/nonexistent")
        system.writes = 0
        system.duplicates = 0
        system.exact_times = True
        system.http_port = 0
        system._client = lambda: Client()
        system.close = lambda: None
        scope = Scope("u", "p", "a")
        turns = (Turn("user", "the same passage"),)
        sessions = [
            Session("a", scope, "2023-05-01T09:00:00Z", turns),
            Session("b", scope, "2023-05-02T09:00:00Z", turns),
        ]
        report = system.seed(sessions)
        self.assertEqual((report.sessions, system.duplicates), (2, 1))
        self.assertIn("1 session(s) refused in part as duplicates", report.notes)


class StageProtocolTests(unittest.TestCase):
    """RUNNING.md fixes stages S4 and T5 before they run; these keep its numbers and choices true."""

    @classmethod
    def setUpClass(cls):
        text = (ROOT / "benchmarks/longhaul/RUNNING.md").read_text()
        start = text.index("## Stage S4: the lived track T2b")
        cls.section = text[start : text.index("## Analysis, fixed before any real run")]

    def test_the_counts_seeds_and_choices_are_declared(self):
        s = self.section
        for phrase in (
            "4 tasks x 4 real sessions x 5 arms x 1 repetition = **80 real sessions**", "fresh **seed 19**",
            "`notes`", "**Label: lived.**", "no test can reach p < 0.05", "**draw seed 20**",
            "5 arms x 60 questions x 2 calls = 600 calls", "**`log`, not `notes`**", "12,000 characters",
            "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442", "exact two-sided McNemar",
            "Holm over four", "never committed", "no re-runs", "waits (up to 90 seconds",
        ):  # fmt: skip
            self.assertIn(phrase, s, phrase)

    def test_the_counts_match_the_driver_and_the_generator(self):
        tasks = lb.lived_tasks(1)
        self.assertEqual(len(tasks), 4)
        self.assertEqual({len(t["prompts"]) for t in tasks}, {4})
        self.assertEqual(len(tasks) * 4 * 5, 80)
        self.assertEqual(5 * 60 * 2, 600)
        spec = importlib.util.spec_from_file_location("s4driver", "/tmp/mk-lh-s4/driver.py")
        if not Path("/tmp/mk-lh-s4/driver.py").exists():
            self.skipTest("the stage drivers live outside the repository")
        driver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(driver)
        self.assertEqual(driver.LIVED_TASKS, [f"t2b-{t['task'].id[4:]}" for t in tasks])
        self.assertEqual(
            (driver.SEED_REAL, driver.DRAW_REAL, driver.SIZE, driver.BUDGET), (19, 20, 60, 12000)
        )
        self.assertEqual(driver.CEILING_S4, 80)
        self.assertEqual(driver.CEILING_T5_CALLS, 600)
        self.assertNotIn("notes", driver.ARMS_T5)

    def test_the_wins_needed_for_a_mcnemar_pair_are_what_the_exact_test_gives(self):
        def needed(losses, factor):
            return min(w for w in range(1, 61) if lb.exact_sign_test(w, losses) * factor < 0.05)

        for losses, raw, holm in ((0, 6, 8), (1, 8, 10), (2, 10, 13), (3, 12, 15)):
            self.assertEqual((needed(losses, 1), needed(losses, 4)), (raw, holm), losses)
        self.assertIn(
            "with 1 loss 8 or 10, with 2 losses 10 or 13, with 3 losses 12 or 15", self.section
        )


if __name__ == "__main__":
    unittest.main()
