"""The task-outcome benchmark's own tests: no model, no network, every arm offline."""

import argparse
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
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
AGENT_MCP = AGENT + " --mcp-config {mcp_config}"


AGENT_SETUP = AGENT_MCP + " --settings {settings}"


def agent_for(arm):
    return {"mcp": AGENT_MCP, "setup": AGENT_SETUP}.get(arm, AGENT)


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
    """The whole flow with the stub agent, in all five arms."""

    TASKS = ("legacy-rounding", "alert-sink", "wrong-total", "staging-collector")

    def test_stub_pipeline_all_arms(self):
        with tempfile.TemporaryDirectory(prefix="mk-obp-") as tmp:
            results = Path(tmp) / "results"

            def one(args):
                task, arm = args
                workdir = Path(tmp) / f"{task}-{arm}"
                return ob.cmd_run(
                    argparse.Namespace(
                        task=task,
                        arm=arm,
                        agent_command=agent_for(arm),
                        workdir=str(workdir),
                        results=str(results),
                        rep=1,
                        timeout=120,
                    )
                )

            jobs = [(t, a) for t in self.TASKS for a in ob.ARMS]
            with ThreadPoolExecutor(4) as pool:
                outcomes = dict(zip(jobs, pool.map(one, jobs), strict=True))
            for (task, arm), out in outcomes.items():
                self.assertEqual(out["passed"], arm != "off", (task, arm))
                data = json.loads(Path(out["result_file"]).read_text())
                self.assertEqual(data["task"], task)
                self.assertTrue(all(s["exit_code"] == 0 for s in data["sessions"].values()))
                self.assertEqual(data["sessions"]["2"]["input_tokens"], 2000)
                if arm in ob.MEMORY_ARMS:
                    store, between = data["store"], data["store_at_between"]
                    self.assertGreaterEqual(store["observations_captured"], 3)
                    self.assertGreater(between["observations_captured"], 0)
                    self.assertEqual(between["checkpoints_saved"], 1)
                    # Arm setup's start hook also briefs the empty store in session 1.
                    first = 1 if arm == "setup" else 0
                    self.assertEqual(between["briefings_served"], first)
                    self.assertEqual(store["briefings_served"], first + 1)
                else:
                    self.assertIsNone(data["store"])
                    self.assertEqual(data["notes_left"]["left"], arm == "notes")
            # The daemons and stores are gone.
            for task, arm in jobs:
                if arm in ob.MEMORY_ARMS:
                    state = json.loads(
                        (ob.bench_dir(Path(tmp) / f"{task}-{arm}") / "state.json").read_text()
                    )
                    self.assertIsNone(state["daemon_pid"])
                    self.assertFalse(Path(state["store_root"]).exists())
            text = ob.cmd_report(argparse.Namespace(results=str(results), json=False, rep=True))
            self.assertIn("### Over repetitions", text)
            for label in ob.ARM_LABELS.values():
                self.assertIn(label, text)
            agg = ob.aggregate(ob.load_results(results))
            n = len(self.TASKS)
            self.assertEqual(agg["arms"]["off"]["successes"], 0)
            for arm in ("notes", "on", "mcp", "setup"):
                self.assertEqual(agg["arms"][arm]["successes"], n)
            self.assertEqual(len(agg["pairs"]), 10)
            off_vs_on = next(p for p in agg["pairs"] if (p["a"], p["b"]) == ("off", "on"))
            self.assertEqual(off_vs_on["only_b"], n)
            self.assertAlmostEqual(off_vs_on["p_value"], 2 * 0.5**4)
            for arm in ob.MEMORY_ARMS:
                use = agg["memory_use"][arm]
                self.assertEqual(
                    (use["runs"], use["wrote_in_session1"], use["read_in_session2"]), (n, n, n)
                )
            self.assertEqual(agg["notes_left"]["off"]["left_notes"], 0)
            self.assertEqual(agg["notes_left"]["notes"]["left_notes"], n)

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
                    "memoryking save-progress",
                    "memoryking resume",
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
                # The project also comes from the environment, which the CLI now honours.
                progress = run(
                    "save-progress", "--task", "t1", "--objective", "o", "--state", "half done"
                )
                self.assertEqual(progress.returncode, 0, progress.stderr)
                self.assertIn("half done", run("resume", "--task", "t1").stdout)
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
                self.assertEqual(result["store"]["checkpoints_saved"], 1)
            finally:
                if state.get("daemon_pid"):
                    ob.stop_daemon(state)
                if state.get("store_root"):
                    shutil.rmtree(state["store_root"], ignore_errors=True)


class ArmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mk-oba-")
        self.addCleanup(self.tmp.cleanup)

    def prepare(self, arm, task="alert-sink"):
        workdir = Path(self.tmp.name) / f"{task}-{arm}"
        out = ob.cmd_prepare(argparse.Namespace(task=task, arm=arm, workdir=str(workdir), rep=1))
        state = ob.load_state(workdir)
        self.addCleanup(self.cleanup_arm, workdir)
        return workdir, out, state

    @staticmethod
    def cleanup_arm(workdir):
        state = ob.load_state(workdir)
        ob.stop_daemon(state)
        if state.get("store_root"):
            shutil.rmtree(state["store_root"], ignore_errors=True)

    @staticmethod
    def prompts(out):
        return Path(out["session1_prompt"]).read_text(), Path(out["session2_prompt"]).read_text()

    def test_notes_arm_is_off_plus_a_notes_file_and_one_sentence(self):
        off_dir, off_out, _ = self.prepare("off")
        workdir, out, state = self.prepare("notes")
        self.assertEqual(listing(workdir), sorted([*listing(off_dir), "NOTES.md"]))
        self.assertLess(len((workdir / "NOTES.md").read_text().splitlines()), 6)
        self.assertFalse((workdir / "MEMORY.md").exists())
        self.assertFalse(state.get("store_root"))
        for plain, noted in zip(self.prompts(off_out), self.prompts(out), strict=True):
            self.assertEqual(
                noted,
                plain.rstrip("\n")
                + "\n\nYou can keep notes for later sessions in NOTES.md in this directory.\n",
            )
        self.assertEqual(ob.agent_env(state), {})

    def test_mcp_arm_has_tools_through_a_config_outside_the_workdir_and_nothing_else(self):
        off_dir, off_out, _ = self.prepare("off")
        workdir, out, state = self.prepare("mcp")
        self.assertEqual(listing(workdir), listing(off_dir))
        bench = ob.bench_dir(workdir)
        self.assertFalse((bench / "bin").exists())
        self.assertEqual(out["mcp_config"], str(bench / "mcp.json"))
        self.assertEqual(oct((bench / "mcp.json").stat().st_mode & 0o777), "0o600")
        server = json.loads((bench / "mcp.json").read_text())["mcpServers"]["memoryking"]
        self.assertEqual(server["command"], sys.executable)
        args = server["args"]
        self.assertEqual(args[:2], ["-m", "memoryking.adapters.stdio"])
        self.assertEqual(args[args.index("--profile") + 1], "lean")
        self.assertEqual(args[args.index("--project") + 1], "work")
        self.assertEqual(
            args[args.index("--socket") + 1], str(Path(state["data"]) / "service.sock")
        )
        self.assertTrue(Path(args[args.index("--credential") + 1]).is_file())
        for plain, with_tools in zip(self.prompts(off_out), self.prompts(out), strict=True):
            self.assertEqual(
                with_tools,
                plain.rstrip("\n")
                + "\n\nThis project has memory tools available (remember, recall, save_progress, "
                "resume); using them is allowed.\n",
            )
            self.assertNotIn("MEMORY.md", with_tools)
        # The agent's environment has no memoryking command and no MemoryKing variables.
        with unittest.mock.patch.dict(os.environ, {"MEMORYKING_PROFILE": "leak"}):
            env = ob.merged_env(state)
            self.assertIn("unset MEMORYKING_PROFILE", ob.env_lines(state))
        self.assertFalse(any(k.startswith("MEMORYKING_") for k in env))
        for entry in env["PATH"].split(os.pathsep):
            self.assertFalse(os.access(os.path.join(entry, "memoryking"), os.X_OK), entry)
        # The credential is the scoped agent one: no administrative capability.
        listed = ob.memoryking(
            state, "credential", "list", "--json", credential=state["owner_credential"]
        ).stdout
        self.assertIn("checkpoint", listed)

    def test_mcp_arm_needs_the_config_placeholder_and_other_arms_refuse_it(self):
        mcp_dir, _, _ = self.prepare("mcp")
        with self.assertRaises(ob.BenchmarkError):
            ob.cmd_session(
                argparse.Namespace(workdir=str(mcp_dir), n=1, agent_command="true", timeout=30)
            )
        off_dir, _, _ = self.prepare("off")
        with self.assertRaises(ob.BenchmarkError):
            ob.cmd_session(
                argparse.Namespace(
                    workdir=str(off_dir), n=1, agent_command="true {mcp_config}", timeout=30
                )
            )

    def test_setup_arm_is_what_memoryking_setup_installs_and_the_prompt_says_nothing(self):
        off_dir, off_out, _ = self.prepare("off")
        workdir, out, state = self.prepare("setup")
        self.assertEqual(listing(workdir), listing(off_dir))
        bench = ob.bench_dir(workdir)
        # The prompts are the off arm's: the agent is not told about memory.
        self.assertEqual(self.prompts(out), self.prompts(off_out))
        self.assertEqual(out["settings"], str(bench / "settings.json"))
        self.assertEqual(out["mcp_config"], str(bench / "mcp.json"))
        for name in ("settings.json", "mcp.json"):
            self.assertEqual(oct((bench / name).stat().st_mode & 0o777), "0o600")
        # The bridge entry is setup's: lean profile, always loaded, the one agent credential.
        server = json.loads((bench / "mcp.json").read_text())["mcpServers"]["memoryking"]
        self.assertTrue(server["alwaysLoad"])
        self.assertEqual(server["type"], "stdio")
        self.assertEqual(server["args"][:3], ["-m", "memoryking", "mcp"])
        self.assertEqual(server["args"][server["args"].index("--profile") + 1], "lean")
        # The hooks are setup's: three events, same credential, the tools named at start.
        hooks = json.loads((bench / "settings.json").read_text())["hooks"]
        self.assertEqual(sorted(hooks), ["PreCompact", "SessionEnd", "SessionStart"])
        commands = {e: hooks[e][0]["hooks"][0]["command"] for e in hooks}
        credential = server["args"][server["args"].index("--credential") + 1]
        for event, command in commands.items():
            self.assertIn(json.dumps(credential), command, event)
            self.assertIn("--host claude", command, event)
            self.assertNotIn("--profile outcomes", command, event)  # no profile in the host's home
        self.assertIn("--tools mcp", commands["SessionStart"])
        self.assertNotIn("--tools", commands["SessionEnd"])
        # No memoryking command or variable for the agent, exactly like the mcp arm.
        self.assertEqual(ob.agent_env(state)["PATH"], ob.path_without_memoryking())
        # Both placeholders are required, and only this arm has {settings}.
        for command in ("true", "true {mcp_config}", "true {settings}"):
            with self.assertRaises(ob.BenchmarkError, msg=command):
                ob.cmd_session(
                    argparse.Namespace(workdir=str(workdir), n=1, agent_command=command, timeout=30)
                )
        mcp_dir, _, _ = self.prepare("mcp")
        with self.assertRaises(ob.BenchmarkError):
            ob.cmd_session(
                argparse.Namespace(
                    workdir=str(mcp_dir),
                    n=1,
                    agent_command="true {mcp_config} {settings}",
                    timeout=30,
                )
            )

    def test_the_setup_arm_carries_every_tasks_knowledge_through_hooks_alone(self):
        # The stub never recalls or saves what it learned: the start hook's briefing in
        # session 2 is all it has, and it must hold each task's carried facts.
        for task in ob.all_task_ids():
            workdir, _, _ = self.prepare("setup", task)
            for n in (1, 2):
                done = ob.cmd_session(
                    argparse.Namespace(
                        workdir=str(workdir), n=n, agent_command=AGENT_SETUP, timeout=120
                    )
                )
                self.assertEqual(done["exit_code"], 0, (task, n))
            context = (ob.bench_dir(workdir) / "context2.md").read_text()
            self.assertIn("mcp__memoryking__remember", context)
            self.assertTrue(ob.cmd_check(argparse.Namespace(workdir=str(workdir)))["passed"], task)

    def test_the_stub_really_speaks_mcp_to_the_bridge(self):
        workdir, _, _ = self.prepare("mcp", "staging-collector")
        for n in (1, 2):
            ob.cmd_session(
                argparse.Namespace(workdir=str(workdir), n=n, agent_command=AGENT_MCP, timeout=120)
            )
            if n == 1:
                ob.cmd_between(argparse.Namespace(workdir=str(workdir)))
        state = ob.load_state(workdir)
        self.assertTrue(all(s["exit_code"] == 0 for s in state["sessions"].values()))
        self.assertGreaterEqual(state["store_at_between"]["observations_captured"], 3)
        self.assertEqual(ob.store_statistics(state)["briefings_served"], 1)
        self.assertTrue(ob.cmd_check(argparse.Namespace(workdir=str(workdir)))["passed"])


class NotesDetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mk-obn-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name) / "base"
        self.work = Path(self.tmp.name) / "work"
        (self.base / "pkg").mkdir(parents=True)
        (self.base / "pkg/mod.py").write_text('"""Module."""\n\n\ndef f(x):\n    return x + 1\n')
        (self.base / "README.md").write_text("# Project\n")
        (self.base / "data.json").write_text("{}")
        shutil.copytree(self.base, self.work)

    def found(self):
        return ob.detect_notes(self.base, self.work)

    def test_untouched_and_real_work_are_not_notes(self):
        self.assertEqual(self.found(), [])
        (self.work / "pkg/mod.py").write_text('"""Module."""\n\n\ndef f(x):\n    return x + 2\n')
        (self.work / "pkg/new.py").write_text("def g():\n    return 1\n")
        (self.work / "tests.json").write_text("{}")
        (self.work / "data.json").write_text('{"a": 1}')
        (self.work / "pkg/empty.py").write_text("")
        (self.work / "pkg/__pycache__").mkdir()
        (self.work / "pkg/__pycache__/mod.cpython-312.pyc").write_bytes(b"x")
        (self.work / "README.md").unlink()
        self.assertEqual(self.found(), [])

    def test_new_non_code_files_are_notes(self):
        (self.work / "NOTES.md").write_text("tried X, failed")
        (self.work / "scratch.txt").write_text("port is 4222")
        (self.work / "todo").write_text("later")
        self.assertEqual(self.found(), ["NOTES.md", "scratch.txt", "todo"])

    def test_edited_text_files_are_notes(self):
        (self.work / "README.md").write_text("# Project\n\nDo not touch legacy/.\n")
        self.assertEqual(self.found(), ["README.md"])

    def test_comment_and_docstring_only_changes_are_notes(self):
        (self.work / "pkg/mod.py").write_text(
            '"""Module. Never use round(): the vendor truncates."""\n\n\n'
            "# NOTE: the vendor API caps this at 100.\n"
            "def f(x):\n"
            '    """Add one."""\n'
            "    return x + 1  # keep in sync with the feed\n"
        )
        self.assertEqual(self.found(), ["pkg/mod.py"])

    def test_comments_mixed_with_code_changes_are_not_detected(self):
        (self.work / "pkg/mod.py").write_text(
            '"""Module."""\n\n\n# note\ndef f(x):\n    return x + 2\n'
        )
        self.assertEqual(self.found(), [])

    def test_new_python_file_of_only_comments_or_a_docstring_is_notes(self):
        (self.work / "pkg/lessons.py").write_text('"""Tried retries; they hang."""\n# more\n')
        (self.work / "pkg/broken.py").write_text("def (:\n")
        self.assertEqual(self.found(), ["pkg/lessons.py"])

    def test_between_records_what_session_one_left(self):
        with tempfile.TemporaryDirectory(prefix="mk-obq-") as tmp:
            first, second = Path(tmp) / "w", Path(tmp) / "w2"
            for workdir, arm in ((first, "notes"), (second, "off")):
                ob.cmd_prepare(
                    argparse.Namespace(task="alert-sink", arm=arm, workdir=str(workdir), rep=1)
                )
            ob.cmd_between(argparse.Namespace(workdir=str(first)))
            self.assertEqual(ob.load_state(first)["notes_left"], {"left": False, "files": []})
            (second / "NOTES.md").write_text("hello")
            ob.cmd_between(argparse.Namespace(workdir=str(second)))
            self.assertEqual(
                ob.load_state(second)["notes_left"], {"left": True, "files": ["NOTES.md"]}
            )


class AgentCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mk-obc-")
        self.addCleanup(self.tmp.cleanup)
        self.workdir = Path(self.tmp.name) / "w"
        ob.cmd_prepare(
            argparse.Namespace(task="alert-sink", arm="off", workdir=str(self.workdir), rep=1)
        )
        self.bench = ob.bench_dir(self.workdir)

    def session(self, command, **kw):
        ns = argparse.Namespace(workdir=str(self.workdir), n=1, agent_command=command, timeout=30)
        for key, value in kw.items():
            setattr(ns, key, value)
        out = ob.cmd_session(ns)
        return out, ob.load_state(self.workdir)["sessions"]["1"]

    @staticmethod
    def python(code, *extra):
        return f"{sys.executable} -c {shlex.quote(code)} {' '.join(extra)}"

    def test_cwd_placeholders_and_stdout_capture(self):
        code = "import os,sys;print(os.getcwd());print(sys.argv[1]);print(open(sys.argv[2]).read()[:12])"
        out, rec = self.session(self.python(code, "{session}", "{prompt_file}"))
        self.assertEqual(out["exit_code"], 0)
        lines = (self.bench / "agent1.stdout").read_text().splitlines()
        self.assertEqual(Path(lines[0]).resolve(), self.workdir.resolve())
        self.assertEqual(lines[1:], ["1", "Work only in"])
        self.assertNotIn("input_tokens", rec)

    def test_prompt_on_stdin_and_literal_braces_survive(self):
        code = "import sys;print(sys.stdin.read()[:10]);print(sys.argv[1])"
        self.session(self.python(code, "'{\"a\": 1}'"), prompt_stdin=True)
        lines = (self.bench / "agent1.stdout").read_text().splitlines()
        self.assertEqual(lines, ["Work only ", '{"a": 1}'])

    def test_usage_convention_from_printed_json(self):
        code = (
            "import json;print('starting');print(json.dumps({'input_tokens': 120,"
            " 'output_tokens': 30, 'cost_usd': 0.25, 'duration_ms': 4500, 'result': 'done'}))"
        )
        _, rec = self.session(self.python(code))
        self.assertEqual(
            (rec["input_tokens"], rec["output_tokens"], rec["cost_usd"]), (120, 30, 0.25)
        )
        self.assertEqual(rec["agent_reported_seconds"], 4.5)

    def test_usage_file_wins_and_missing_figures_stay_missing(self):
        code = (
            "import json,sys;json.dump({'input_tokens': 5}, open(sys.argv[1], 'w'));"
            "print('{\"input_tokens\": 99}')"
        )
        _, rec = self.session(self.python(code, "{usage_file}"))
        self.assertEqual(rec["input_tokens"], 5)
        self.assertNotIn("output_tokens", rec)
        state = ob.load_state(self.workdir)
        state["sessions"] = {}
        ob.save_state(self.workdir, state)
        _, rec = self.session(self.python("print('no json here')"))
        self.assertNotIn("input_tokens", rec)

    @unittest.skipUnless(shutil.which("jq"), "jq is not installed")
    def test_usage_jq_maps_a_nested_result(self):
        code = (
            "import json;print(json.dumps({'usage': {'input_tokens': 7, 'output_tokens': 3},"
            " 'total_cost_usd': 0.5, 'duration_ms': 2000}))"
        )
        _, rec = self.session(
            self.python(code),
            usage_jq="{input_tokens: .usage.input_tokens, output_tokens: .usage.output_tokens,"
            " cost_usd: .total_cost_usd, duration_seconds: (.duration_ms / 1000)}",
        )
        self.assertEqual((rec["input_tokens"], rec["output_tokens"], rec["cost_usd"]), (7, 3, 0.5))
        self.assertEqual(rec["agent_reported_seconds"], 2.0)

    def test_usage_jq_without_jq_is_refused_before_running(self):
        with unittest.mock.patch.object(ob.shutil, "which", return_value=None):
            with self.assertRaises(ob.BenchmarkError):
                self.session("true", usage_jq=".")
        self.assertFalse((self.bench / "agent1.stdout").exists())

    def test_timeout_stops_the_command_and_its_children(self):
        marker = Path(self.tmp.name) / "child-alive"
        child = f"import time,pathlib;time.sleep(3);pathlib.Path({str(marker)!r}).write_text('x')"
        code = (
            "import subprocess,sys,time;"
            f"subprocess.Popen([sys.executable,'-c',{child!r}]);time.sleep(60)"
        )
        started = time.time()
        out, rec = self.session(self.python(code), timeout=1)
        self.assertLess(time.time() - started, 20)
        self.assertEqual((out["exit_code"], out["timed_out"]), (-9, True))
        self.assertTrue(rec["timed_out"])
        time.sleep(3.5)
        self.assertFalse(marker.exists())

    def test_session_environment_is_the_arms(self):
        code = "import os;print(os.environ.get('MEMORYKING_PROJECT'))"
        with unittest.mock.patch.dict(os.environ, {"MEMORYKING_PROJECT": "leak"}):
            self.session(self.python(code))
        # Arm off changes nothing about the environment; the point of the mcp arm is the
        # stripped environment, tested in ArmTests.
        self.assertEqual((self.bench / "agent1.stdout").read_text().strip(), "leak")


class ReportTests(unittest.TestCase):
    def row(self, task, kind, arm, passed, tokens=None, seconds=None, **extra):
        sessions = {}
        if tokens is not None:
            sessions = {
                "1": {"input_tokens": tokens, "output_tokens": 0, "seconds": seconds},
                "2": {"input_tokens": tokens, "output_tokens": 0, "seconds": seconds},
            }
        return {
            "task": task,
            "kind": kind,
            "arm": arm,
            "passed": passed,
            "sessions": sessions,
            **extra,
        }

    def test_exact_test(self):
        self.assertEqual(ob.exact_sign_test(0, 0), 1.0)
        self.assertAlmostEqual(ob.exact_sign_test(6, 0), 2 / 64)
        self.assertAlmostEqual(ob.exact_sign_test(5, 0), 2 / 32)
        self.assertAlmostEqual(ob.exact_sign_test(3, 1), 0.625)
        self.assertEqual(ob.exact_sign_test(2, 2), 1.0)
        self.assertEqual(ob.exact_sign_test(1, 4), ob.exact_sign_test(4, 1))

    def test_aggregate_and_render_for_four_arms(self):
        rows = []
        passes = {"off": 3, "notes": 5, "on": 8, "mcp": 7}
        scale = {"off": 1, "notes": 1, "on": 2, "mcp": 3}
        for i in range(10):
            kind = ob.KINDS[i % 4]
            for arm, count in passes.items():
                rows.append(
                    self.row(
                        f"t{i}",
                        kind,
                        arm,
                        i < count,
                        tokens=100 * scale[arm],
                        seconds=2.0 * (i + 1),
                    )
                )
        agg = ob.aggregate(rows)
        self.assertEqual(agg["arm_order"], ["off", "notes", "on", "mcp"])
        self.assertEqual({a: v["successes"] for a, v in agg["arms"].items()}, passes)
        self.assertEqual(agg["arms"]["on"]["tokens"]["mean"], 400)
        self.assertEqual(agg["arms"]["on"]["tokens"]["median"], 400)
        self.assertEqual(agg["arms"]["off"]["seconds"]["mean"], 22.0)
        self.assertEqual(agg["arms"]["off"]["seconds"]["median"], 22.0)
        self.assertEqual(agg["arms"]["off"]["cost_usd"]["mean"], None)
        self.assertEqual(len(agg["pairs"]), 6)
        off_on = next(p for p in agg["pairs"] if (p["a"], p["b"]) == ("off", "on"))
        self.assertEqual((off_on["only_a"], off_on["only_b"]), (0, 5))
        self.assertEqual((off_on["both_pass"], off_on["both_fail"]), (3, 2))
        self.assertAlmostEqual(off_on["p_value"], 2 / 32)
        self.assertIn("no difference is shown", off_on["reading"])
        notes_mcp = next(p for p in agg["pairs"] if (p["a"], p["b"]) == ("notes", "mcp"))
        self.assertEqual((notes_mcp["only_a"], notes_mcp["only_b"]), (0, 2))
        self.assertIn("no difference is shown", notes_mcp["reading"])
        self.assertEqual(sum(v["on"]["runs"] for v in agg["per_kind"].values()), 10)
        text = ob.render_markdown(agg)
        self.assertIn("| Off (nothing) | 10 | 3 | 30% |", text)
        self.assertIn("| Memory CLI (on) | 10 | 8 | 80% |", text)
        self.assertIn("Memory CLI (on) vs Memory MCP", text)
        self.assertIn("0.0625", text)
        self.assertIn("Ten tasks can only show a large effect", text)
        self.assertIn("n/a", text)

    def test_repetitions_are_aggregated_by_task_as_well_as_by_run(self):
        rows = []
        # Task t0 always passes with "on" and never with "off"; t1 passes in 2 of 3 reps in
        # both; t2 passes only with "off".
        table = {
            ("on", "t0"): [1, 1, 1],
            ("off", "t0"): [0, 0, 0],
            ("on", "t1"): [1, 1, 0],
            ("off", "t1"): [1, 0, 1],
            ("on", "t2"): [0, 0, 0],
            ("off", "t2"): [1, 1, 1],
        }
        for (arm, task), passed in table.items():
            for rep, ok in enumerate(passed, 1):
                rows.append(self.row(task, "decision", arm, bool(ok), rep=rep))
        rep = ob.rep_aggregate(rows)
        on = rep["arms"]["on"]
        self.assertEqual((on["runs"], on["successes"], on["repetitions"]), (9, 5, [1, 2, 3]))
        self.assertAlmostEqual(on["task_mean_rate"], (1 + 2 / 3 + 0) / 3)
        self.assertEqual((on["tasks_always_passed"], on["tasks_never_passed"]), (1, 1))
        # Resampling tasks gives a wider interval than treating the nine runs as independent.
        wide = on["task_bootstrap95"][1] - on["task_bootstrap95"][0]
        self.assertGreater(wide, on["wilson95"][1] - on["wilson95"][0] - 1e-9)
        self.assertEqual(rep, ob.rep_aggregate(rows))  # deterministic
        (pair,) = rep["pairs"]
        self.assertEqual(pair["by_run"]["pairs"], 9)
        self.assertEqual((pair["by_run"]["only_a"], pair["by_run"]["only_b"]), (4, 4))
        self.assertEqual(
            (pair["by_task"]["a_higher"], pair["by_task"]["b_higher"], pair["by_task"]["tied"]),
            (1, 1, 1),
        )
        self.assertEqual(pair["by_task"]["p_value"], 1.0)
        text = ob.render_rep_markdown(rep)
        self.assertIn("Task-resampled 95%", text)
        self.assertIn("Off (nothing) vs Memory CLI (on)", text)
        with tempfile.TemporaryDirectory(prefix="mk-obr-") as tmp:
            for i, row in enumerate(rows):
                (Path(tmp) / f"r{i}.json").write_text(json.dumps(row))
            out = cli("report", "--results", tmp, "--rep")
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("### Over repetitions", out.stdout)
            plain = cli("report", "--results", tmp)
            self.assertNotIn("Over repetitions", plain.stdout)
            data = json.loads(cli("report", "--results", tmp, "--json", "--rep").stdout)
            self.assertIn("by_rep", data)

    def test_median_differs_from_mean(self):
        rows = [
            self.row("a", "instruction", "off", True, tokens=10, seconds=1),
            self.row("b", "instruction", "off", True, tokens=10, seconds=1),
            self.row("c", "instruction", "off", True, tokens=100, seconds=10),
        ]
        figure = ob.aggregate(rows)["arms"]["off"]["tokens"]
        self.assertEqual((figure["mean"], figure["median"]), (80, 20))

    def test_plain_words_reading(self):
        self.assertIn("exactly the same tasks", ob.reading("off", "on", 0, 0, 1.0))
        self.assertIn("same number", ob.reading("off", "on", 2, 2, 1.0))
        self.assertIn("Off (nothing) passed 6", ob.reading("off", "on", 6, 0, 2 / 64))
        self.assertIn("unlikely to be chance", ob.reading("off", "on", 6, 0, 2 / 64))

    def test_memory_use_comes_from_the_stores_own_counts(self):
        def store(obs, checkpoints, briefings, deliveries):
            return {
                "observations_captured": obs,
                "checkpoints_saved": checkpoints,
                "briefings_served": briefings,
                "deliveries": deliveries,
            }

        rows = [
            # wrote and read
            self.row(
                "a",
                "instruction",
                "on",
                True,
                store=store(5, 1, 3, 4),
                store_at_between=store(4, 1, 1, 1),
            ),
            # wrote but never read in session 2
            self.row(
                "b",
                "instruction",
                "on",
                False,
                store=store(2, 0, 0, 0),
                store_at_between=store(2, 0, 0, 0),
            ),
            # read in session 2 but wrote nothing in session 1
            self.row(
                "c",
                "instruction",
                "on",
                False,
                store=store(0, 0, 2, 0),
                store_at_between=store(0, 0, 0, 0),
            ),
            # an old result without the between figures
            self.row("d", "instruction", "on", False, store=store(1, 0, 0, 0)),
        ]
        use = ob.aggregate(rows)["memory_use"]["on"]
        self.assertEqual(
            use, {"runs": 4, "wrote_in_session1": 2, "read_in_session2": 2, "unknown": 1}
        )
        self.assertIn("| Memory CLI (on) | 4 | 2 | 2 | 1 |", ob.render_markdown(ob.aggregate(rows)))

    def test_notes_left_counts_for_off_and_notes_only(self):
        rows = [
            self.row(
                "a", "decision", "off", False, notes_left={"left": True, "files": ["NOTES.md"]}
            ),
            self.row("b", "decision", "off", False, notes_left={"left": False, "files": []}),
            self.row("c", "decision", "off", False),
            self.row(
                "a", "decision", "notes", True, notes_left={"left": True, "files": ["NOTES.md"]}
            ),
            self.row("a", "decision", "on", True),
        ]
        agg = ob.aggregate(rows)
        self.assertEqual(agg["notes_left"]["off"], {"runs": 3, "left_notes": 1, "unknown": 1})
        self.assertEqual(agg["notes_left"]["notes"], {"runs": 1, "left_notes": 1, "unknown": 0})
        self.assertNotIn("on", agg["notes_left"])
        self.assertIn("Notes left on disk", ob.render_markdown(agg))

    def test_a_single_arm_and_missing_figures_still_report(self):
        agg = ob.aggregate([self.row("a", "instruction", "on", True)])
        self.assertEqual(agg["pairs"], [])
        self.assertIsNone(agg["arms"]["on"]["tokens"]["mean"])
        self.assertIn("n/a", ob.render_markdown(agg))

    def test_report_cli_needs_results(self):
        with tempfile.TemporaryDirectory(prefix="mk-obr-") as tmp:
            self.assertEqual(cli("report", "--results", tmp).returncode, 2)


if __name__ == "__main__":
    unittest.main()
