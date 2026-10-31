"""Every error code has an actionable hint, replies carry it, and the doc matches."""

import json
import re
import subprocess
import sys
import threading
import unittest
from pathlib import Path

from test_journal import observation

from memoryking.adapters.local_ipc import Server, request
from memoryking.adapters.local_store import LocalStore
from memoryking.domain.hints import FAMILIES, GENERIC, HINTS, hint
from memoryking.domain.local import LocalError

ROOT = Path(__file__).resolve().parents[1]


class HintTableTests(unittest.TestCase):
    def test_every_raised_code_has_an_exact_hint(self):
        codes = set()
        for path in (ROOT / "src/memoryking").rglob("*.py"):
            codes.update(re.findall(r'LocalError\("([a-z0-9_]+)"', path.read_text()))
        missing = sorted(code for code in codes if code not in HINTS)
        self.assertEqual(missing, [], "add these to memoryking.domain.hints")
        for code, text in HINTS.items():
            self.assertTrue(text.endswith((".", "?")), code)
            self.assertLess(len(text), 200, code)
            self.assertNotIn("\n", text, code)

    def test_families_and_generic_cover_unknown_codes(self):
        self.assertEqual(hint("made_up_conflict"), FAMILIES[0][1])
        self.assertEqual(hint("entirely_new"), GENERIC)
        self.assertEqual(hint("denied"), HINTS["denied"])

    def test_generated_document_matches_the_table(self):
        run = subprocess.run(
            [sys.executable, str(ROOT / "tools/generate_error_codes.py"), "--check"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


class HintDeliveryTests(unittest.TestCase):
    def test_socket_and_cli_replies_carry_the_hint(self):
        import tempfile

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            credential = LocalStore.initialize(base / "store", base / "control")
            with LocalStore(base / "store", base / "control") as store, Server(store) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with self.assertRaises(LocalError) as caught:
                        request(server.path, "not-a-credential", "read", {"project": "demo"})
                    self.assertEqual(caught.exception.code, "invalid_token")
                    self.assertEqual(caught.exception.hint, HINTS["invalid_token"])
                    ns = store.namespace(credential.read_text(), "demo")
                    store.capture(credential.read_text(), ns, "k", observation(1))
                    run = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "memoryking",
                            "status",
                            "--socket",
                            str(server.path),
                            "--credential",
                            str(base / "missing.credential"),
                        ],
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(run.returncode, 2)
                    body = json.loads(run.stdout.strip() or run.stderr.strip())
                    self.assertIn("hint", body)
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
