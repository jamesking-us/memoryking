"""Tests for what stage T6 needed in the harness before its run: the draw that leaves out earlier draws (and
leaves the old draws as they were), the released 0.2.0 arm, the call cap, the dry mode and the driver. No
model call and no network. Tests that need the third-party data file, the released virtualenv or the other
products skip when they are absent; none of them draws seed 20 or above on the real data.
"""

import importlib.util
import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))

from longhaul import lme  # noqa: E402
from longhaul.systems import memoryking_020 as released  # noqa: E402

if sys.platform == "win32":
    raise unittest.SkipTest("the benchmark harness runs on Linux and macOS")

PY = sys.executable
FAKE = ROOT / "benchmarks/longhaul/fake_model.py"
CLI = ROOT / "tools/longhaul_benchmark.py"
DRIVER = ROOT / "tools/longhaul_t6.py"
RUNNING = ROOT / "benchmarks/longhaul/RUNNING.md"

SPEC = importlib.util.spec_from_file_location("longhaul_t6_driver", DRIVER)
driver = importlib.util.module_from_spec(SPEC)
sys.modules["longhaul_t6_driver"] = driver
SPEC.loader.exec_module(driver)

TYPES = {
    "alpha": 90,
    "beta": 70,
    "gamma": 50,
    "delta": 40,
    "epsilon": 30,
    "zeta": 20,
}  # 300 questions


def synthetic(n_scale: int = 1) -> list[dict]:
    return [
        {"question_id": f"{t}-{i:03d}", "question_type": t}
        for t, n in TYPES.items()
        for i in range(n * n_scale)
    ]


def reference_select(data, size=60, seed=1):
    """The draw as it was written for stage T5, copied here so a change to `lme.select` cannot go
    unnoticed."""
    types: dict[str, list[dict]] = {}
    for item in data:
        types.setdefault(item["question_type"], []).append(item)
    names = sorted(types)
    total = len(data)
    exact = {t: size * len(types[t]) / total for t in names}
    take = {t: max(1, math.floor(exact[t])) for t in names}
    for t in sorted(names, key=lambda t: (exact[t] - math.floor(exact[t]), t), reverse=True):
        if sum(take.values()) >= size:
            break
        take[t] += 1
    while sum(take.values()) > size:
        t = max(names, key=lambda t: (take[t] - exact[t], t))
        take[t] -= 1
    chosen = []
    for t in names:
        pool = sorted(types[t], key=lambda i: i["question_id"])
        rng = random.Random(f"{seed}:{t}")
        chosen.extend(rng.sample(pool, min(take[t], len(pool))))
    return sorted(chosen, key=lambda i: i["question_id"])


# SHA-256 of the sorted ids of the development draws (size 60, seeds 1 to 5) of the cleaned LongMemEval `_s`
# file, as stage T5 and its development checks drew them.
DEV_DIGESTS = {
    1: "17292b65174b9ccdffe7d77124762b929d0ee8404104379abfb6f0545c7bf974",
    2: "0ee9a682936b3a7f7f4ade6caeff5f3c05aadb0694d98941f821caa27ee713a4",
    3: "c4a42397625a8c19fdc4c11cb90752f983cb2bb6d24bfb254043ffbfab05d730",
    4: "7a38dc833a1f59ad7d4db451829fa70712134c47394b1823d962f6e9770f2c98",
    5: "512ec8eff1619ae406486a8f88514b47afcf88504f85f2f37921fe503e0f6556",
}
DATA_SHA = "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"


class DrawTests(unittest.TestCase):
    def test_the_existing_draws_are_what_they_were(self):
        data = synthetic()
        for seed in (1, 2, 3, 4, 5, 20):
            for size in (60, 20, 7):
                self.assertEqual(lme.select(data, size, seed), reference_select(data, size, seed))

    def test_the_development_draws_of_the_real_file_are_what_they_were(self):
        if not lme.DEFAULT_DATA.exists():
            self.skipTest("the LongMemEval file is not on this machine")
        data, digest = lme.load(lme.DEFAULT_DATA)
        if digest != DATA_SHA:
            self.skipTest("another data file")
        for seed, want in DEV_DIGESTS.items():  # development seeds only, never 20 or 21
            self.assertEqual(lme.ids_digest(lme.select(data, 60, seed)), want)

    def test_a_draw_leaves_out_what_it_is_told_to_and_follows_the_rule(self):
        data = synthetic()
        earlier = lme.earlier_ids(data, ((1, 60), (2, 60), (3, 30)))
        pool = [i for i in data if i["question_id"] not in earlier]
        got = lme.select_excluding(data, 40, 9, earlier)
        self.assertEqual(len(got), 40)
        self.assertFalse({g["question_id"] for g in got} & earlier)
        self.assertEqual([g["question_id"] for g in got], sorted(g["question_id"] for g in got))
        # the shares of the types follow the pool, by the same rule as `select`
        self.assertEqual(
            sorted(g["question_id"] for g in got),
            sorted(g["question_id"] for g in reference_select(pool, 40, 9)),
        )
        # excluding nothing is the plain draw
        self.assertEqual(lme.select_excluding(data, 40, 9, set()), lme.select(data, 40, 9))

    def test_the_draw_does_not_depend_on_the_order_of_the_file(self):
        data = synthetic()
        shuffled = list(reversed(data))
        random.Random(3).shuffle(shuffled)
        earlier = lme.earlier_ids(data, ((1, 60),))
        self.assertEqual(
            lme.select_excluding(data, 30, 4, earlier),
            lme.select_excluding(shuffled, 30, 4, earlier),
        )

    def test_a_type_with_too_few_questions_left_is_refused_not_shrunk(self):
        data = synthetic()
        left = {i["question_id"] for i in data[:100]}
        gone = {i["question_id"] for i in data} - left
        with self.assertRaises(ValueError) as cm:
            lme.select_excluding(data, 120, 1, gone)  # 120 asked, 100 left
        self.assertIn("fewer than", str(cm.exception))

    def test_the_stage_draw_refuses_a_pool_other_than_the_declared_one(self):
        # the real constants are for the real file; this one is not it
        data = synthetic()
        with self.assertRaises(ValueError) as cm:
            lme.t6_draw(data, 12, 21)
        self.assertIn("declared", str(cm.exception))

    def test_the_stage_constants(self):
        self.assertEqual(lme.T6_EARLIER, tuple((s, 60) for s in (1, 2, 3, 4, 5, 20)))
        self.assertEqual((lme.T6_SIZE, lme.T6_SEED, lme.T6_POOL), (120, 21, 232))
        self.assertEqual(
            lme.ids_digest([{"question_id": "b"}, {"question_id": "a"}]),
            lme.ids_digest([{"question_id": "a"}, {"question_id": "b"}]),
        )


class ReleasedArmTests(unittest.TestCase):
    def fake_venv(self, base: Path, version: str) -> Path:
        venv = base / "venv"
        (venv / "bin").mkdir(parents=True)
        py = venv / "bin/python"
        py.write_text(f"#!/bin/sh\necho 'memoryking {version}'\n")
        py.chmod(0o755)
        return venv

    def test_a_missing_environment_says_how_to_make_it(self):
        base = Path(tempfile.mkdtemp(dir="/tmp", prefix="mk-t6-"))
        self.addCleanup(shutil.rmtree, base, True)
        os.environ["LONGHAUL_MEMORYKING_020"] = str(base / "none")
        self.addCleanup(os.environ.pop, "LONGHAUL_MEMORYKING_020", None)
        with self.assertRaises(RuntimeError) as cm:
            released.create()
        self.assertIn("install_systems.py memoryking-0.2.0", str(cm.exception))

    def test_another_version_stops_the_arm(self):
        base = Path(tempfile.mkdtemp(dir="/tmp", prefix="mk-t6-"))
        self.addCleanup(shutil.rmtree, base, True)
        os.environ["LONGHAUL_MEMORYKING_020"] = str(self.fake_venv(base, "0.2.1"))
        self.addCleanup(os.environ.pop, "LONGHAUL_MEMORYKING_020", None)
        with self.assertRaises(RuntimeError) as cm:
            released.create()
        self.assertIn("0.2.1", str(cm.exception))

    def test_the_lock_pins_the_released_wheel_and_every_dependency_by_hash(self):
        text = (ROOT / "benchmarks/longhaul/tools/locks/memoryking-0.2.0.lock").read_text()
        self.assertIn("memoryking==0.2.0", text)
        requirements = [x for x in text.split("\n") if x and x[0].isalpha()]
        self.assertGreater(len(requirements), 10)
        for entry in text.replace(" \\\n", " ").splitlines():
            if entry and entry[0].isalpha():
                self.assertIn("--hash=sha256:", entry, entry)
        self.assertNotIn("git+", text)

    def test_the_arm_drives_the_released_package_not_this_checkout(self):
        venv = released.venv_dir()
        if not (venv / "bin/python").is_file():
            self.skipTest(
                "the released MemoryKing is not installed (install_systems.py memoryking-0.2.0)"
            )
        base = Path(tempfile.mkdtemp(dir="/tmp", prefix="mk-t6-"))
        self.addCleanup(shutil.rmtree, base, True)
        system = released.create()
        self.assertEqual(system.version, "0.2.0")
        try:
            system.prepare(base / "a")  # starts the worker, which opens a store of 0.2.0
            where = subprocess.run(
                [str(venv / "bin/python"), "-I", "-c", "import memoryking; print(memoryking.__file__)"],
                capture_output=True, text=True, cwd="/",
            ).stdout.strip()  # fmt: skip
            self.assertTrue(where.startswith(str(venv.resolve()) + os.sep), where)
            self.assertNotIn(str(ROOT / "src"), where)
        finally:
            system.close()
        self.assertTrue((base / "a/store/data").exists())
        self.assertIsNone(system.proc)

    def test_the_worker_refuses_another_version_or_a_package_outside_its_environment(self):
        venv = released.venv_dir()
        if not (venv / "bin/python").is_file():
            self.skipTest("the released MemoryKing is not installed")
        for expected, where in (("9.9.9", venv), ("0.2.0", Path("/nonexistent-venv"))):
            proc = subprocess.run(
                [
                    str(venv / "bin/python"),
                    "-I",
                    str(released.WORKER),
                    str(ROOT),
                    expected,
                    str(where),
                ],
                input="",
                capture_output=True,
                text=True,
                cwd="/",
                timeout=60,
            )
            self.assertEqual(proc.returncode, 3, proc.stderr)
            self.assertIn("wrong memoryking", proc.stderr)

    def test_the_arm_is_loadable_by_name(self):
        from longhaul.systems import load

        self.assertTrue(callable(load) and hasattr(released, "create"))


def fake_data():
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


def cli(*args, check_env=None):
    return subprocess.run(
        [PY, str(CLI), *map(str, args)], capture_output=True, text=True, timeout=300,
        env={**os.environ, "PYTHONPATH": ""},
    )  # fmt: skip


class CapAndDryTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(dir="/tmp", prefix="mk-t6-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.data = self.base / "data.json"
        self.data.write_text(json.dumps(fake_data()))
        self.ledger = self.base / "ledger.jsonl"

    def run_arm(self, arm, cap, ledger=True, extra=()):
        return cli(
            "t5-run", "--arm", arm, "--data", self.data, "--size", 6,
            "--reader-command", f"{PY} {FAKE} reader", "--judge-command", f"{PY} {FAKE} judge",
            "--results", self.base / "res", "--work", self.base / "w", "--budget-chars", 4000,
            *(["--ledger", self.ledger] if ledger else []), "--call-cap", cap, *extra,
        )  # fmt: skip

    def starts(self):
        if not self.ledger.exists():
            return 0
        return sum(
            json.loads(x)["ev"] in ("reader_start", "judge_start")
            for x in self.ledger.read_text().splitlines()
        )

    def test_no_call_is_made_beyond_the_cap_and_earlier_invocations_count(self):
        first = self.run_arm("log", 5)
        self.assertEqual(first.returncode, 2, first.stdout)
        self.assertIn("cap of 5", first.stderr)
        self.assertEqual(self.starts(), 5)
        second = self.run_arm("log", 5)  # a new invocation: the five calls made before still count
        self.assertEqual(second.returncode, 2)
        self.assertEqual(self.starts(), 5)
        third = self.run_arm("notes", 5)  # another arm shares the ledger and so the cap
        self.assertEqual(third.returncode, 2)
        self.assertEqual(self.starts(), 5)

    def test_a_cap_needs_a_ledger(self):
        proc = self.run_arm("log", 5, ledger=False)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("needs a ledger", proc.stderr)

    def test_arms_running_together_cannot_pass_the_cap(self):
        procs = [
            subprocess.Popen(
                [PY, str(CLI), "t5-run", "--arm", arm, "--data", str(self.data), "--size", "6",
                 "--reader-command", f"{PY} {FAKE} reader", "--judge-command", f"{PY} {FAKE} judge",
                 "--results", str(self.base / "res"), "--work", str(self.base / "w"),
                 "--budget-chars", "4000", "--ledger", str(self.ledger), "--call-cap", "17"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env={**os.environ, "PYTHONPATH": ""},
            )
            for arm in ("log", "notes", "off")
        ]  # fmt: skip
        codes = [p.wait(timeout=300) for p in procs]
        for p in procs:
            p.stdout.close()
            p.stderr.close()
        self.assertEqual(self.starts(), 17)  # 3 arms x 12 calls = 36 wanted; exactly 17 allowed
        self.assertTrue(all(c == 2 for c in codes) or 0 in codes)

    def test_a_finished_arm_is_not_asked_again_when_the_run_is_started_again(self):
        ok = self.run_arm("log", 100)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(self.starts(), 12)
        (self.base / "res/t5/log-6.json").unlink()
        again = self.run_arm("log", 12)  # the cap equals the calls made: any call would be refused
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.starts(), 12)

    def test_a_dry_run_makes_no_call_and_says_so(self):
        proc = cli("t5-run", "--arm", "log", "--dry", "--data", self.data, "--size", 6, "--seed", 1,
                   "--results", self.base / "res", "--work", self.base / "w", "--budget-chars", 4000,
                   "--ledger", self.ledger)  # fmt: skip
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.starts(), 0)
        result = json.loads((self.base / "res/t5/log-6-dry.json").read_text())
        self.assertTrue(result["dry"])
        self.assertTrue(all(r["dry"] and not r["lost_call"] for r in result["rows"]))
        self.assertTrue(all(r["evidence"]["answer_turns"] == 1 for r in result["rows"]))
        self.assertFalse(
            (self.base / "res/t5/log-6.json").exists()
        )  # never mistaken for a real result

    def test_a_dry_run_refuses_commands_stage_draws_and_a_real_run_needs_them(self):
        common = ["--data", self.data, "--size", 6, "--results", self.base / "res",
                  "--work", self.base / "w"]  # fmt: skip
        with_cmd = cli("t5-run", "--arm", "log", "--dry", "--reader-command", "true", *common)
        self.assertEqual(with_cmd.returncode, 2)
        stage = cli("t5-run", "--arm", "log", "--dry", "--seed", 20, *common)
        self.assertEqual(stage.returncode, 2)
        self.assertIn("development draw", stage.stderr)
        t6 = cli("t5-run", "--arm", "log", "--dry", "--t6", *common)
        self.assertEqual(t6.returncode, 2)
        real = cli("t5-run", "--arm", "log", *common)
        self.assertEqual(real.returncode, 2)
        limit = cli("t5-run", "--arm", "log", "--limit", 1, "--reader-command", "true",
                    "--judge-command", "true", *common)  # fmt: skip
        self.assertEqual(limit.returncode, 2)
        self.assertEqual(self.starts(), 0)

    def test_a_dry_run_limit_takes_the_first_questions_of_the_draw(self):
        proc = cli("t5-run", "--arm", "log", "--dry", "--limit", 2, "--data", self.data, "--size", 6,
                   "--seed", 1, "--results", self.base / "res", "--work", self.base / "w")  # fmt: skip
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads((self.base / "res/t5/log-6-dry.json").read_text())
        self.assertEqual(result["questions"], 2)

    def test_the_selection_record_has_the_digest_of_the_ids(self):
        proc = cli("t5-select", "--data", self.data, "--size", 6, "--seed", 1)
        out = json.loads(proc.stdout)
        self.assertEqual(out["ids_sha256"], lme.ids_digest(lme.select(fake_data(), 6, 1)))


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(dir="/tmp", prefix="mk-t6-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.data = self.base / "data.json"
        self.data.write_text(json.dumps(fake_data()))

    def drive(self, *args, root=None):
        return subprocess.run(
            [PY, str(DRIVER), "--root", str(root or self.base / "root"), *map(str, args)],
            capture_output=True, text=True, timeout=600, env={**os.environ, "PYTHONPATH": ""},
        )  # fmt: skip

    def stub(self, *args, cap=None, root=None):
        base = ["--stub", "--draw-seed", "1", "--size", "6", "--arms", "log,notes,off",
                "--data", str(self.data)]  # fmt: skip
        if cap is not None:
            base += ["--cap", str(cap)]
        return self.drive(*base, *args, root=root)

    def ledger(self):
        path = self.base / "root/stub-s1/ledger.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()]

    def test_the_stage_numbers(self):
        self.assertEqual(
            driver.ARMS,
            ["basic_memory", "memoryking_020", "memoryking_semantic", "mcp_memory_service"],
        )
        self.assertEqual((driver.SIZE, driver.DRAW_SEED, driver.BUDGET), (120, 21, 12000))
        self.assertEqual(driver.CEILING, 960)
        self.assertEqual(driver.CEILING, 4 * 120 * 2)
        self.assertEqual(driver.PARALLEL, 4)
        self.assertEqual(driver.VERSIONS["memoryking_020"], "0.2.0")

    def test_a_real_run_needs_the_go_ahead_for_a_clean_commit_and_the_declared_numbers(self):
        no_go = self.drive("run")
        self.assertNotEqual(no_go.returncode, 0)
        self.assertIn("go-ahead", no_go.stderr)
        wrong = self.drive("--size", 60, "run")
        self.assertIn("stage T6 is draw 21", wrong.stderr)
        self.assertFalse((self.base / "root/real-t6/ledger.jsonl").exists())
        stage_seed = self.drive("--stub", "--draw-seed", 21, "run")
        self.assertIn("development draw", stage_seed.stderr)
        stage_seed = self.drive("--stub", "--draw-seed", 20, "run")
        self.assertIn("development draw", stage_seed.stderr)

    def test_the_real_run_cannot_change_arms_or_the_cap(self):
        self.assertIn("all four arms", self.drive("--arms", "log", "run").stderr)
        self.assertIn("all four arms", self.drive("--cap", 1000, "run").stderr)

    def test_the_stub_run_finishes_resumes_and_never_repeats_a_call(self):
        done = self.stub("run")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("stage done", done.stdout)
        calls = [e for e in self.ledger() if e["ev"] in ("reader_start", "judge_start")]
        self.assertEqual(len(calls), 3 * 6 * 2)
        self.assertEqual(len({(e["arm"], e["qid"], e["ev"]) for e in calls}), len(calls))
        for arm in ("log", "notes", "off"):  # an arm is started again with its result file gone
            (self.base / f"root/stub-s1/results/t5/{arm}-6.json").unlink()
        again = self.stub("run")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(
            len([e for e in self.ledger() if e["ev"] in ("reader_start", "judge_start")]), 36
        )
        status = self.stub("status")
        self.assertIn("reader calls 18, judge calls 18", status.stdout)

    def test_the_cap_stops_arms_and_the_fault_is_listed(self):
        run = self.stub("run", cap=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("stopped with arms unfinished", run.stdout)
        self.assertEqual(
            len([e for e in self.ledger() if e["ev"] in ("reader_start", "judge_start")]), 10
        )
        status = self.stub("status", cap=10)
        self.assertIn("fault", status.stdout)
        self.assertIn("cap of 10", status.stdout)
        again = self.stub("run", cap=10)  # an earlier invocation's calls count
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("cap is spent", again.stderr)

    def test_plan_names_the_960_calls(self):
        plan = self.drive("plan")
        self.assertIn("4 arms x 120 questions x 2 calls = 960 calls", plan.stdout)
        self.assertIn("cap 960", plan.stdout)


class ProtocolTextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        text = RUNNING.read_text()
        start = text.index("## Stage T6:")
        cls.section = text[start : text.index("## Analysis, fixed before any real run")]

    def test_the_stage_is_declared_with_its_numbers_arms_and_analysis(self):
        for phrase in (
            "draw seed 21", "232 of the 500", "960", "MemoryKing 0.2.0 as released", "Holm", "exact two-sided sign test",
            "primary comparison", "Wilson", "basic_memory", "mcp_memory_service", "12,000 characters",
            "tools/longhaul_t6.py", "GO-T6", "--version", "Only harness faults",
            "What the design can and cannot show", "Expected duration",
        ):  # fmt: skip
            self.assertIn(phrase, self.section, phrase)

    def test_the_commit_and_time_were_filled_in_at_the_go_ahead(self):
        self.assertNotIn("TO BE FILLED IN AT THE GO-AHEAD", self.section)
        self.assertIn("83f6ff90bf5b1d76d99cb46a98f248be970101ee", self.section)
        self.assertIn("this line was written 2026-11-27 19:46 UTC", self.section)


if __name__ == "__main__":
    unittest.main()
