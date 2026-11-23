"""Tests for stage T5b: the `memoryking` arm with the semantic channel on (`memoryking_semantic`).
No model call. The test that runs the real embedding model is skipped where the optional runtime or the
installed model is missing.
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
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))

from longhaul.systems import load as load_system  # noqa: E402
from longhaul.systems import memoryking as mk  # noqa: E402
from longhaul.systems.base import Scope, Session, Turn  # noqa: E402

if sys.platform == "win32":
    raise unittest.SkipTest("the benchmark harness runs on Linux and macOS")

PY = sys.executable
FAKE = ROOT / "benchmarks/longhaul/fake_model.py"
MODELS = Path(os.environ.get("LONGHAUL_MEMORYKING_MODELS", "/tmp/mk-models"))


class ArmOptionTests(unittest.TestCase):
    def test_the_semantic_arm_is_a_named_option_and_the_default_arm_is_unchanged(self):
        plain, semantic = load_system("memoryking"), load_system("memoryking_semantic")
        self.assertEqual(
            (plain.name, plain.semantic, plain.via_daemon), ("memoryking", False, False)
        )
        self.assertEqual(
            (semantic.name, semantic.semantic, semantic.via_daemon),
            ("memoryking_semantic", True, True),
        )
        self.assertEqual(semantic.version, plain.version)
        with mock.patch.dict(os.environ, {"LONGHAUL_MEMORYKING_MODELS": "/m"}):
            self.assertEqual(semantic._env()["MEMORYKING_MODELS"], "/m")
            self.assertNotIn("MEMORYKING_MODELS", {k for k in plain._env() if k not in os.environ})

    def test_the_receipt_trace_says_whether_the_semantic_channel_contributed(self):
        note = mk.MemoryKing._dense_note
        ready = {
            "model_fingerprints": ["abc"],
            "trace": [
                {
                    "stage": "dense",
                    "availability": "ready",
                    "visible_candidates": 256,
                    "eligible_vectors": 494,
                },
                {"stage": "rank", "fusion": {"dense": 1, "lexical": 1, "method": "convex"}},
            ],
        }
        got = note(ready)
        self.assertTrue(got["semantic_contributed"])
        self.assertEqual(got["fusion"]["method"], "convex")
        self.assertEqual(got["visible_candidates"], 256)
        for reason in ("no_model_loaded", "project_not_enabled", "query_embedding_failed"):
            off = note(
                {"trace": [{"stage": "dense", "availability": "unavailable", "reason": reason}]}
            )
            self.assertEqual(off, {"semantic_contributed": False, "reason": reason})
        self.assertEqual(
            note({"trace": [{"stage": "lexical"}]})["reason"], "no_dense_stage_in_trace"
        )
        no_model = dict(ready, model_fingerprints=[])
        self.assertFalse(note(no_model)["semantic_contributed"])

    def test_a_briefing_without_the_semantic_channel_stops_the_arm_loudly(self):
        system = mk.create_semantic()
        system.socket_path = None
        scope = Scope("u", "p", "a")
        with (
            mock.patch.object(mk.MemoryKing, "_start_daemon"),
            mock.patch.object(mk.MemoryKing, "_credential", return_value=Path("/c")),
            mock.patch.object(mk.hooks, "recall", return_value={"text": "x", "receipt": "r"}),
            mock.patch.object(
                mk.MemoryKing,
                "_receipt_diagnostics",
                return_value={
                    "trace": [
                        {
                            "stage": "dense",
                            "availability": "unavailable",
                            "reason": "project_not_enabled",
                        }
                    ]
                },
            ),
            mock.patch.object(mk.MemoryKing, "socket", Path("/s")),
        ):
            with self.assertRaises(RuntimeError) as caught:
                system.context_for("q", scope, 1000)
        self.assertIn("project_not_enabled", str(caught.exception))
        plain = mk.create()
        with (
            mock.patch.object(mk.MemoryKing, "_start_daemon"),
            mock.patch.object(mk.MemoryKing, "_credential", return_value=Path("/c")),
            mock.patch.object(mk.hooks, "recall", return_value={"text": "kept", "receipt": "r"}),
            mock.patch.object(mk.MemoryKing, "socket", Path("/s")),
        ):
            self.assertEqual(
                plain.context_for("q", scope, 1000), "kept"
            )  # keyword-only: no check, no extra request
            self.assertEqual(plain.retrieval_note, {})


@unittest.skipUnless(
    (MODELS / "bge-small-en-v1-5-onnx").is_dir(),
    "the verified model is not installed at LONGHAUL_MEMORYKING_MODELS",
)
class RealModelTests(unittest.TestCase):
    def test_a_small_haystack_is_indexed_waited_for_and_found_through_the_semantic_channel(self):
        from memoryking import semantic as sem

        with mock.patch.dict(os.environ, {"MEMORYKING_MODELS": str(MODELS)}):
            problem = sem.runtime_problem()
            if problem or not sem.verified():
                self.skipTest(problem or "the model does not verify")
        base = Path(tempfile.mkdtemp(dir="/tmp", prefix="mk-lhs5-"))
        self.addCleanup(shutil.rmtree, base, True)
        scope = Scope("lme-user", "lme", "lme-agent")
        sessions = [
            Session(
                f"s{i}",
                scope,
                f"2023-05-0{i + 1}T09:00:00Z",
                (
                    Turn(
                        "user",
                        f"I adopted a retriever puppy named Biscuit number {i} last spring and we walk daily.",
                    ),
                    Turn("assistant", "Lovely, dogs need daily walks."),
                ),
            )
            for i in range(3)
        ]
        system = mk.create_semantic()
        system.ingest = "turns"
        system.prepare(base / "s")
        try:
            system.seed(sessions)
            settled = system.before_retrieval(scope)
            self.assertGreater(settled["vectors"], 0)
            self.assertEqual(settled["model"], "bge-small-en-v1-5-onnx")
            text = system.context_for("what is the name of my dog", scope, 4000)
            self.assertIn("Biscuit", text)
            self.assertTrue(system.retrieval_note["semantic_contributed"])
            self.assertGreater(system.retrieval_note["visible_candidates"], 0)
        finally:
            system.close()


class RepeatedSessionIdTests(unittest.TestCase):
    def test_a_haystack_that_repeats_a_session_id_with_another_date_is_ingested(self):
        base = Path(tempfile.mkdtemp(dir="/tmp", prefix="mk-lhs5-"))
        self.addCleanup(shutil.rmtree, base, True)
        scope = Scope("u", "p", "a")
        turns = (Turn("user", "my cat is called Mochi"), Turn("assistant", "Nice name."))
        sessions = [
            Session("same", scope, "2023-05-01T09:00:00Z", turns),
            Session("same", scope, "2023-06-01T09:00:00Z", turns),
        ]
        system = mk.create()
        system.ingest = "turns"
        system.prepare(base / "s")
        try:
            report = system.seed(sessions)  # was: idempotency_conflict on the second session
            self.assertEqual(report.sessions, 2)
            self.assertIn("Mochi", system.context_for("what is my cat called", scope, 2000))
        finally:
            system.close()


class RunnerTests(unittest.TestCase):
    def test_the_t5_runner_records_the_index_wait_and_the_retrieval_note_per_question(self):
        base = Path(tempfile.mkdtemp(dir="/tmp", prefix="mk-lhs5-"))
        self.addCleanup(shutil.rmtree, base, True)
        data = []
        for t in ("single-session-user", "multi-session", "knowledge-update"):
            for i in range(2):
                data.append(
                    {
                        "question_id": f"{t}-{i}", "question_type": t, "question": f"What is the code for {t[:3]}{i}?",
                        "answer": f"{100 + i}", "question_date": "2023/05/30 (Tue) 10:00",
                        "answer_session_ids": [f"{t}-{i}-s1"],
                        "haystack_session_ids": [f"{t}-{i}-s0", f"{t}-{i}-s1"],
                        "haystack_dates": ["2023/05/01 (Mon) 09:00", "2023/05/02 (Tue) 09:00"],
                        "haystack_sessions": [[{"role": "user", "content": "Hello."}],
                                              [{"role": "user", "content": f"The code for {t[:3]}{i} is {100 + i}.", "has_answer": True}]],
                    }
                )  # fmt: skip
        (base / "d.json").write_text(json.dumps(data))
        proc = subprocess.run(
            [PY, str(ROOT / "tools/longhaul_benchmark.py"), "t5-run", "--arm", "log", "--data", str(base / "d.json"), "--size", "6",
             "--reader-command", f"{PY} {FAKE} reader", "--judge-command", f"{PY} {FAKE} judge", "--results", str(base / "r"),
             "--work", str(base / "w"), "--budget-chars", "4000"],
            capture_output=True, text=True, timeout=300, env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        )  # fmt: skip
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = json.loads((base / "r/t5/log-6.json").read_text())["rows"]
        self.assertTrue(
            all(r["index"] == {} and r["retrieval_note"] == {} for r in rows)
        )  # `log` has neither


class ProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        text = (ROOT / "benchmarks/longhaul/RUNNING.md").read_text()
        start = text.index("## Stage T5b: track T5 again")
        cls.section = text[start : text.index("## Analysis, fixed before any real run")]

    def test_the_stage_is_declared_with_its_counts_gate_and_analysis(self):
        for phrase in (
            "a second run of one arm after a declared change of configuration", "the keyword-only result of stage T5 stands",
            "`memoryking_semantic`", "**seed 20**", "**12,000 characters**", "120 calls, the ceiling", "/tmp/mk-lh-s4/GO-T5b",
            "**Holm over five**", "against keyword-only `memoryking`", "nothing was tuned", "not run again",
            "fails loudly if the semantic channel is not used", "waits until the project's index is complete",
            "no_model_loaded", "project_not_enabled",
        ):  # fmt: skip
            self.assertIn(phrase, self.section, phrase)

    def test_the_driver_counts_and_gate(self):
        path = Path("/tmp/mk-lh-s4/driver.py")
        if not path.exists():
            self.skipTest("the stage drivers live outside the repository")
        spec = importlib.util.spec_from_file_location("s5driver", path)
        driver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(driver)
        self.assertEqual(driver.ARMS_T5B, ["memoryking_semantic"])
        self.assertEqual(driver.CEILING_T5B_CALLS, 120)
        self.assertEqual(1 * 60 * 2, driver.CEILING_T5B_CALLS)


if __name__ == "__main__":
    unittest.main()
