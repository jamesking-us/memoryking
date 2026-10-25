import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "report_evidence", Path(__file__).resolve().parents[1] / "tools/report_evidence.py"
)
evidence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evidence)


class EvidenceTests(unittest.TestCase):
    def test_snippet_uses_commit_not_changed_working_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)

            def git(*args):
                return (
                    subprocess.check_output(["git", "-C", folder, *args], stderr=subprocess.DEVNULL)
                    .decode()
                    .strip()
                )

            git("init")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.invalid")
            (root / "code.py").write_text("first\nsecond\nthird\n")
            git("add", "code.py")
            git("commit", "-m", "fixture")
            commit = git("rev-parse", "HEAD")
            (root / "code.py").write_text("tampered working file\n")
            result = evidence.snippet(root, commit, "code.py", 2, 3)
            self.assertEqual(result["text"], "second\nthird\n")
            with self.assertRaises(ValueError):
                evidence.snippet(root, commit, "code.py", 1, 4)
            with self.assertRaises(ValueError):
                evidence.snippet(root, "HEAD", "code.py", 1, 1)

    def test_manifest_rejects_outside_paths_and_private_ledgers(self):
        with tempfile.TemporaryDirectory() as folder:
            for path in ["../outside", ".phase-reports/ledger.json", ".env", "/tmp/outside"]:
                with self.assertRaises(ValueError):
                    evidence.manifest(Path(folder), [path])
