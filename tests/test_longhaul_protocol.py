"""The rules RUNNING.md fixes before the next run of the long-history benchmark, as tests.

What the pilot found cannot come back (working directory, session persistence, the seeded
digest, hidden checks that fail correct code), and the protocol's mechanics (development and
measured seeds, regrading saved outputs, products measured on the lived track only, probes,
budgets) behave as the text says. No model is called. Every directory is unique to this run and
the T4 listener ports are chosen free (`test_longhaul_benchmark.pick_port_base`).
"""

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_longhaul_benchmark as tb  # noqa: E402  (sets LONGHAUL_T4_PORT_BASE for this process)

lb, PY, STUB, ROOT = tb.lb, tb.PY, tb.STUB, tb.ROOT
from longhaul.systems import load as load_system  # noqa: E402


def row(task, arm, passed, **extra):
    base = tb.ReportTests.row(None, task, arm, passed)
    base.update(extra)
    return base


class ProtocolAmendmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = Path(tempfile.mkdtemp(prefix="mk-lha-", dir="/tmp"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    def cli(self, *args):
        return subprocess.run(
            [PY, str(ROOT / "tools/longhaul_benchmark.py"), *map(str, args)],
            capture_output=True,
            text=True,
            timeout=300,
        )

    def run_one(self, results, work, seeded, mode="oracle", task="t1-export-delimiter", rep=1):
        proc = self.cli(
            "run", "--track", "T1", "--size", 20, "--task", task, "--arm", "off", "--rep", rep,
            "--agent", f"{STUB} --mode {mode}", "--seeded", seeded, "--results", results,
            "--work", work,
        )  # fmt: skip
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    # ---- seeds

    def test_the_measured_seed_is_not_a_development_seed_and_development_runs_are_not_reported(
        self,
    ):
        self.assertNotIn(lb.MEASURED_SEED, lb.DEVELOPMENT_SEEDS)
        self.assertIn(1, lb.DEVELOPMENT_SEEDS)  # the pilot's
        results = self.base / "seeds"
        (results / "runs").mkdir(parents=True)
        (results / "runs" / "dev.json").write_text(
            json.dumps(row("t", "off", True, history_seed=1))
        )
        (results / "runs" / "measured.json").write_text(
            json.dumps(row("t2", "off", True, history_seed=lb.MEASURED_SEED))
        )
        parse = lb.build_parser().parse_args
        agg = lb.cmd_report(parse(["report", "--results", str(results), "--json"]))
        self.assertEqual(agg["tracks"]["T1"]["arms"]["off"]["runs"], 1)
        self.assertEqual(agg["development_runs_left_out"], 1)
        text = lb.cmd_report(parse(["report", "--results", str(results)]))
        self.assertIn("development data and are not reported", text)
        every = lb.cmd_report(
            parse(["report", "--results", str(results), "--json", "--include-development"])
        )
        self.assertEqual(every["tracks"]["T1"]["arms"]["off"]["runs"], 2)

    # ---- hidden-check faults

    def test_a_fault_only_in_a_hidden_check_is_corrected_by_regrading_the_saved_outputs(self):
        results, work, seeded = self.base / "rg", self.base / "rgw", self.base / "rgs"
        file = Path(self.run_one(results, work, seeded)["result_file"])
        saved = json.loads(file.read_text())
        self.assertTrue(saved["passed"])
        # the verdict an older, faulty check recorded for code that deserved a pass
        file.write_text(json.dumps({**saved, "passed": False, "failure_class": "missing"}))
        dry = json.loads(self.cli("regrade", "--results", results).stdout)
        self.assertEqual([c["run"] for c in dry["regraded"]], [file.name])
        self.assertEqual((dry["regraded"][0]["was"], dry["regraded"][0]["now"]), (False, True))
        self.assertFalse(
            json.loads(file.read_text())["passed"]
        )  # nothing is written without --apply
        done = json.loads(self.cli("regrade", "--results", results, "--apply").stdout)
        self.assertEqual(len(done["regraded"]), 1)
        fixed = json.loads(file.read_text())
        self.assertTrue(fixed["passed"])
        self.assertEqual(fixed["regraded_from"], {"passed": False, "failure_class": "missing"})
        again = json.loads(self.cli("regrade", "--results", results).stdout)
        self.assertEqual((again["regraded"], again["unchanged"]), ([], 1))
        # code that really is wrong stays wrong
        self.run_one(results, work, seeded, mode="null", task="t1-id-format")
        after = json.loads(self.cli("regrade", "--results", results).stdout)
        self.assertEqual((after["regraded"], after["unchanged"]), ([], 2))

    def test_a_corrected_check_does_not_make_a_seeded_store_unusable(self):
        """The pilot had to rewrite the digest in every seed record: the sessions have their own."""
        seeded = self.base / "digest-seeded"
        proc = self.cli("seed", "--track", "T1", "--size", 20, "--arm", "notes", "--seeded", seeded)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        record = seeded / "notes/T1-20-g0/seed-report.json"
        data = json.loads(record.read_text())
        s = lb.suite("T1", 20, 1)
        self.assertEqual(data["history_digest"], s.history_digest())
        self.assertNotEqual(data["history_digest"], s.digest())
        data["digest"] = "a digest from before the check was fixed"
        record.write_text(json.dumps(data))
        common = ["--track", "T1", "--size", 20, "--task", "t1-id-format", "--arm", "notes"]
        common += ["--agent", f"{STUB} --mode null", "--seeded", seeded]
        common += ["--results", self.base / "digest-r", "--work", self.base / "digest-w"]
        self.assertEqual(self.cli("run", *common).returncode, 0)
        data["history_digest"] = "not this history"
        record.write_text(json.dumps(data))
        proc = self.cli("run", *common, "--rep", 2)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not the one this seed", proc.stderr)

    # ---- products measured on the lived track only

    def test_a_product_whose_real_sessions_would_not_give_it_the_history_is_lived_track_only(self):
        for arm in (
            "basic_memory",
            "basic_memory_mcp",
            "mcp_memory_service",
            "mcp_memory_service_mcp",
        ):
            self.assertIs(load_system(arm).seeding_faithful, False, arm)
        for arm in ("off", "notes", "log", "native", "memoryking", "claude_mem"):
            self.assertTrue(getattr(load_system(arm), "seeding_faithful", True), arm)
        proc = self.cli(
            "seed", "--track", "T1", "--size", 20, "--arm", "mcp_memory_service",
            "--seeded", self.base / "lived-only",
        )  # fmt: skip
        self.assertEqual(proc.returncode, 2)
        self.assertIn("lived track only", proc.stderr)
        self.assertFalse((self.base / "lived-only").exists())

    def test_unfaithful_seeding_is_labelled_in_the_report(self):
        rows = [
            row("t", "memoryking", True, seeding_faithful=True),
            row("t", "basic_memory", False, seeding_faithful=False),
        ]
        text = lb.render_markdown(lb.aggregate(rows))
        self.assertIn("basic_memory (explicit-save upper bound)", text)
        self.assertNotIn("memoryking (explicit", text)

    # ---- the needle's author

    def test_the_author_of_the_needle_is_recorded_per_run_and_splits_the_report(self):
        results, work, seeded = self.base / "by", self.base / "byw", self.base / "bys"
        out = self.run_one(results, work, seeded)
        saved = json.loads(Path(out["result_file"]).read_text())
        task = lb.find_task(lb.suite("T1", 20, 1), "t1-export-delimiter")[1]
        self.assertEqual(saved["stated_by"], task.stated_by)
        rows = [
            row(f"t{i}", "off", i % 2 == 0, stated_by=("user", "assistant")[i % 2])
            for i in range(4)
        ]
        agg = lb.aggregate(rows)
        self.assertEqual(
            agg["tracks"]["T1"]["by_stated_by"],
            {
                "assistant": {"off": {"runs": 2, "passed": 0}},
                "user": {"off": {"runs": 2, "passed": 2}},
            },
        )
        self.assertIn("By who stated the needle", lb.render_markdown(agg))

    # ---- what the pilot fixed

    def test_the_pilots_working_directory_fault_stays_fixed_for_every_file_arm(self):
        """claude-mem and MCP Memory Service take the project from the directory name: it is
        the task's project under `ws/`, never `work`, and no parent holds a CLAUDE.md."""
        for arm in ("off", "notes", "log", "native"):
            seeded = self.base / f"wd-s-{arm}"
            work = self.base / f"wd-w-{arm}"
            results = self.base / f"wd-r-{arm}"
            seed = self.cli("seed", "--track", "T1", "--size", 20, "--arm", arm, "--seeded", seeded)
            self.assertEqual(seed.returncode, 0, seed.stderr)
            agent = "sh -c 'basename {workdir} > {workdir}/wd.txt; basename $(dirname {workdir}) > {workdir}/up.txt'"
            proc = self.cli(
                "run", "--track", "T1", "--size", 20, "--task", "t1-export-delimiter", "--arm", arm,
                "--agent", agent, "--seeded", seeded, "--results", results, "--work", work,
            )  # fmt: skip
            self.assertEqual(proc.returncode, 0, proc.stderr)
            final = results / f"raw/T1-20-t1-export-delimiter-{arm}-r1/final"
            self.assertEqual((final / "wd.txt").read_text().strip(), "ledger-sync", arm)
            self.assertEqual((final / "up.txt").read_text().strip(), "ws", arm)
            self.assertFalse((work / "CLAUDE.md").exists())

    def test_the_agent_command_keeps_session_persistence_on_for_every_arm(self):
        script = (ROOT / "benchmarks/longhaul/agent.sh").read_text()
        self.assertIn("exec claude", script)
        self.assertNotIn("--no-session-persistence", script.split("exec claude", 1)[1])

    # ---- ports

    def test_the_t4_listener_says_so_when_its_port_is_taken(self):
        with socket.socket() as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            port = holder.getsockname()[1]
            with self.assertRaises(lb.BenchmarkError) as caught:
                lb.Collector(port)
        self.assertIn("could not bind", str(caught.exception))
        free = lb.Collector(0)  # a free port of the system's choosing
        free.stop()

    # ---- budgets and the written protocol

    def test_every_arm_has_a_probe_checklist_row_in_the_protocol(self):
        text = (ROOT / "benchmarks/longhaul/RUNNING.md").read_text()
        table = text.split("### Probe checklist", 1)[1].split("\n## ", 1)[0]
        arms = (
            "off", "notes", "log", "native", "memoryking", "mcp_memory", "basic_memory",
            "mcp_memory_service", "claude_mem", "mem0", "graphiti",
        )  # fmt: skip
        for arm in arms:
            self.assertIn(f"`{arm}`", table, arm)
        self.assertIn("1 real session per arm", table)

    def test_the_protocol_states_each_amendment(self):
        text = (ROOT / "benchmarks/longhaul/RUNNING.md").read_text()
        for phrase in (
            f"seed {lb.MEASURED_SEED}",
            "development data",
            "regrade",
            "re-run",
            "LONGHAUL_CLAUDE_MEM_CALL_CAP",
            "2,310",
            "ws/<project>",
            "lived track only",
        ):
            self.assertIn(phrase, text, phrase)


if __name__ == "__main__":
    unittest.main()
