"""The task-outcome benchmark's own tests: no model, no network, both arms offline."""

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "outcome_benchmark", ROOT / "tools/outcome_benchmark.py"
)
ob = importlib.util.module_from_spec(SPEC)
sys.modules["outcome_benchmark"] = ob
SPEC.loader.exec_module(ob)

STUB = ROOT / "benchmarks/outcomes/stub_agent.py"
AGENT = f"{sys.executable} {STUB} {{workdir}} {{prompt_file}} --usage-file {{usage_file}}"
CLEAN = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}


def cli(*args):
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools/outcome_benchmark.py"), *map(str, args)],
        capture_output=True,
        text=True,
        env=CLEAN,
        timeout=120,
    )
    return proc


def listing(root: Path):
    return sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    )


class TaskFixtureTests(unittest.TestCase):
    def test_every_task_is_complete(self):
        ids = ob.all_task_ids()
        self.assertEqual(len(ids), 10)
        kinds = {}
        for task_id in ids:
            task_dir, meta = ob.load_task(task_id)
            self.assertEqual(meta["id"], task_id)
            for name in ("session1.md", "session2.md", "check.py", "meta.json"):
                self.assertTrue((task_dir / name).is_file(), (task_id, name))
            self.assertTrue(os.access(task_dir / "check.py", os.X_OK), task_id)
            self.assertTrue((task_dir / "reference" / "s2").is_dir(), task_id)
            self.assertTrue((task_dir / "reference_wrong" / "s2").is_dir(), task_id)
            self.assertEqual(meta["between"], (task_dir / "between.sh").is_file(), task_id)
            for key in ("learn", "recall_query", "must_recall"):
                self.assertTrue(meta["stub"][key], (task_id, key))
            self.assertIn(meta["kind"], ob.KINDS)
            kinds.setdefault(meta["kind"], []).append(task_id)
            lines = sum(
                len(p.read_text().splitlines())
                for p in (task_dir / "repo").rglob("*.py")
                if "__pycache__" not in p.parts
            )
            self.assertTrue(95 <= lines <= 450, (task_id, lines))
            for name in ("session1.md", "session2.md"):
                text = (task_dir / name).read_text().lower()
                for word in ("benchmark", "memoryking", "hidden", "check.py"):
                    self.assertNotIn(word, text, (task_id, name))
        for kind in ob.KINDS:
            self.assertGreaterEqual(len(kinds.get(kind, [])), 2, kind)

    def test_every_task_discriminates(self):
        """Fails untouched and after session 1; passes the reference; fails the wrong one."""
        with ThreadPoolExecutor(4) as pool:
            rows = list(pool.map(ob.validate_task, ob.all_task_ids()))
        for row in rows:
            self.assertTrue(row["ok"], row)


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mk-obt-")
        self.addCleanup(self.tmp.cleanup)

    def prepare(self, task, arm="off"):
        workdir = Path(self.tmp.name) / f"{task}-{arm}"
        out = ob.cmd_prepare(argparse.Namespace(task=task, arm=arm, workdir=str(workdir), rep=1))
        return workdir, out

    def test_workdir_holds_only_the_fixture(self):
        for task_id in ob.all_task_ids():
            workdir, out = self.prepare(task_id)
            self.assertEqual(listing(workdir), listing(ob.TASKS / task_id / "repo"), task_id)
            names = " ".join(listing(workdir))
            for secret in ("check.py", "meta.json", "reference", "session1", "session2"):
                self.assertNotIn(secret, names)
            self.assertFalse((workdir / "MEMORY.md").exists())
            bench = ob.bench_dir(workdir)
            self.assertFalse(str(bench).startswith(str(workdir) + os.sep))
            self.assertTrue(Path(out["session1_prompt"]).is_file())

    def test_prompts_carry_scope_and_only_their_own_session(self):
        workdir, out = self.prepare("alert-sink")
        first = Path(out["session1_prompt"]).read_text()
        second = Path(out["session2_prompt"]).read_text()
        self.assertTrue(first.startswith(ob.SCOPE_LINE))
        self.assertIn("alerts/rules.py", first)
        self.assertNotIn("alerts/dispatch.py", first)
        self.assertIn("alerts/dispatch.py", second)
        self.assertNotIn("MEMORY.md", first + second)

    def test_refuses_a_used_directory(self):
        workdir, _ = self.prepare("alert-sink")
        with self.assertRaises(ob.BenchmarkError):
            ob.cmd_prepare(
                argparse.Namespace(task="alert-sink", arm="off", workdir=str(workdir), rep=1)
            )
        with self.assertRaises(ob.BenchmarkError):
            ob.load_task("no-such-task")

    def test_session_records_exit_code_and_time(self):
        workdir, _ = self.prepare("alert-sink")
        result = ob.cmd_session(
            argparse.Namespace(workdir=str(workdir), n=1, agent_command="false", timeout=30)
        )
        self.assertEqual(result["exit_code"], 1)
        result = ob.cmd_session(
            argparse.Namespace(
                workdir=str(workdir),
                n=2,
                agent_command="no-such-agent-binary {workdir}",
                timeout=30,
            )
        )
        self.assertEqual(result["exit_code"], 127)

    def test_between_runs_the_task_step(self):
        workdir, _ = self.prepare("wrong-total")
        self.assertTrue((workdir / "data/prod_sample.json").exists())
        out = ob.cmd_between(argparse.Namespace(workdir=str(workdir)))
        self.assertTrue(out["between_script"])
        self.assertFalse((workdir / "data").exists())
        self.assertFalse((workdir / "tools").exists())
        self.assertTrue((workdir / "export/pipeline.py").exists())


class PipelineTests(unittest.TestCase):
    """The whole flow with the stub agent, in both arms."""

    TASKS = ("legacy-rounding", "alert-sink", "wrong-total", "staging-collector")

    def test_stub_pipeline_both_arms(self):
        with tempfile.TemporaryDirectory(prefix="mk-obp-") as tmp:
            results = Path(tmp) / "results"

            def one(args):
                task, arm = args
                workdir = Path(tmp) / f"{task}-{arm}"
                return ob.cmd_run(
                    argparse.Namespace(
                        task=task,
                        arm=arm,
                        agent_command=AGENT,
                        workdir=str(workdir),
                        results=str(results),
                        rep=1,
                        timeout=120,
                    )
                )

            jobs = [(t, a) for t in self.TASKS for a in ("on", "off")]
            with ThreadPoolExecutor(4) as pool:
                outcomes = dict(zip(jobs, pool.map(one, jobs), strict=True))
            for (task, arm), out in outcomes.items():
                self.assertEqual(out["passed"], arm == "on", (task, arm))
                data = json.loads(Path(out["result_file"]).read_text())
                self.assertEqual(data["task"], task)
                self.assertTrue(all(s["exit_code"] == 0 for s in data["sessions"].values()))
                self.assertEqual(data["sessions"]["2"]["input_tokens"], 2000)
                if arm == "on":
                    store = data["store"]
                    self.assertGreaterEqual(store["observations_captured"], 3)
                    self.assertEqual(store["briefings_served"], 1)
                    self.assertIn("checkpoints_saved", store)
                else:
                    self.assertIsNone(data["store"])
            # The daemons and stores are gone.
            for task, arm in jobs:
                if arm == "on":
                    state = json.loads(
                        (ob.bench_dir(Path(tmp) / f"{task}-{arm}") / "state.json").read_text()
                    )
                    self.assertIsNone(state["daemon_pid"])
                    self.assertFalse(Path(state["store_root"]).exists())
            text = ob.cmd_report(argparse.Namespace(results=str(results), json=False))
            self.assertIn("Memory on", text)
            agg = ob.aggregate(ob.load_results(results))
            self.assertEqual(agg["arms"]["on"]["successes"], len(self.TASKS))
            self.assertEqual(agg["arms"]["off"]["successes"], 0)
            self.assertEqual(agg["paired"]["only_memory_on"], len(self.TASKS))
            self.assertAlmostEqual(agg["paired"]["p_value"], 2 * 0.5**4)

    def test_memory_arm_setup_is_what_a_user_would_have(self):
        with tempfile.TemporaryDirectory(prefix="mk-obm-") as tmp:
            workdir = Path(tmp) / "work"
            out = cli("prepare", "--task", "staging-collector", "--arm", "on", "--workdir", workdir)
            self.assertEqual(out.returncode, 0, out.stderr)
            state = {}
            try:
                state = json.loads((ob.bench_dir(workdir) / "state.json").read_text())
                note = (workdir / "MEMORY.md").read_text()
                for phrase in (
                    "memoryking capture",
                    "memoryking recall",
                    "--project work",
                    "Resume",
                    "Save progress",
                ):
                    self.assertIn(phrase, note)
                self.assertNotIn(state["store_root"], note)
                self.assertNotIn(state["agent_credential"], note)
                self.assertIn(
                    "MEMORY.md", Path(json.loads(out.stdout)["session1_prompt"]).read_text()
                )
                # The CLI works through the environment alone, with a scoped credential.
                env = {**CLEAN}
                for line in cli("env", "--workdir", workdir).stdout.splitlines():
                    key, _, value = line.removeprefix("export ").partition("=")
                    env[key] = value.strip("'")
                run = lambda *a: subprocess.run(  # noqa: E731
                    ["memoryking", *a], capture_output=True, text=True, env=env, timeout=60
                )
                saved = run("capture", "--project", "work", "--text", "Rule: use the retry helper")
                self.assertEqual(saved.returncode, 0, saved.stderr)
                recalled = run("recall", "--project", "work", "--query", "retry helper rule")
                self.assertIn("use the retry helper", recalled.stdout)
                # The agent credential cannot administer.
                denied = run(
                    "credential",
                    "issue",
                    "other",
                    "--project",
                    "work",
                    "--capabilities",
                    "read",
                    "--output",
                    f"{state['store_root']}/x.credential",
                )
                self.assertNotEqual(denied.returncode, 0)
                self.assertEqual(cli("between", "--workdir", workdir).returncode, 0)
                self.assertEqual(cli("check", "--workdir", workdir).returncode, 0)
                done = cli("finish", "--workdir", workdir, "--results", Path(tmp) / "r")
                self.assertEqual(done.returncode, 0, done.stderr)
                result = json.loads((Path(tmp) / "r/staging-collector-on.json").read_text())
                self.assertFalse(result["passed"])
                self.assertEqual(result["store"]["observations_captured"], 1)
            finally:
                if state.get("daemon_pid"):
                    ob.stop_daemon(state)
                if state.get("store_root"):
                    shutil.rmtree(state["store_root"], ignore_errors=True)


class ReportTests(unittest.TestCase):
    def row(self, task, kind, arm, passed, tokens=None, seconds=None):
        sessions = {}
        if tokens is not None:
            sessions = {
                "1": {"input_tokens": tokens, "output_tokens": 0, "seconds": seconds},
                "2": {"input_tokens": tokens, "output_tokens": 0, "seconds": seconds},
            }
        return {"task": task, "kind": kind, "arm": arm, "passed": passed, "sessions": sessions}

    def test_exact_test(self):
        self.assertEqual(ob.exact_sign_test(0, 0), 1.0)
        self.assertAlmostEqual(ob.exact_sign_test(6, 0), 2 / 64)
        self.assertAlmostEqual(ob.exact_sign_test(5, 0), 2 / 32)
        self.assertAlmostEqual(ob.exact_sign_test(3, 1), 0.625)
        self.assertEqual(ob.exact_sign_test(2, 2), 1.0)
        self.assertEqual(ob.exact_sign_test(1, 4), ob.exact_sign_test(4, 1))

    def test_aggregate_and_render(self):
        rows = []
        for i in range(10):
            kind = ob.KINDS[i % 4]
            rows.append(self.row(f"t{i}", kind, "on", i < 8, tokens=100, seconds=2.0))
            rows.append(self.row(f"t{i}", kind, "off", i < 3, tokens=300, seconds=5.0))
        agg = ob.aggregate(rows)
        self.assertEqual(agg["arms"]["on"]["successes"], 8)
        self.assertEqual(agg["arms"]["off"]["successes"], 3)
        self.assertEqual(agg["arms"]["on"]["mean_tokens"], 200)
        self.assertEqual(agg["arms"]["off"]["mean_seconds"], 10.0)
        self.assertEqual(
            (
                agg["paired"]["only_memory_on"],
                agg["paired"]["only_memory_off"],
                agg["paired"]["both_pass"],
                agg["paired"]["both_fail"],
            ),
            (5, 0, 3, 2),
        )
        self.assertAlmostEqual(agg["paired"]["p_value"], 2 / 32)
        self.assertEqual(sum(v["on"]["runs"] for v in agg["per_kind"].values()), 10)
        text = ob.render_markdown(agg)
        self.assertIn("| Memory on | 10 | 8 | 80% |", text)
        self.assertIn("p = 0.0625", text)
        self.assertIn("Ten tasks can only show a large effect", text)

    def test_missing_figures_are_reported_as_unavailable(self):
        agg = ob.aggregate(
            [self.row("a", "instruction", "on", True), self.row("a", "instruction", "off", False)]
        )
        self.assertIsNone(agg["arms"]["on"]["mean_tokens"])
        self.assertIn("n/a", ob.render_markdown(agg))

    def test_report_cli_needs_results(self):
        with tempfile.TemporaryDirectory(prefix="mk-obr-") as tmp:
            self.assertEqual(cli("report", "--results", tmp).returncode, 2)


if __name__ == "__main__":
    unittest.main()
