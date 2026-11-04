"""The Markdown vault: export for hand editing, import as versions and proposals, CLI."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from memoryking import vault
from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
from memoryking.memory_tool import MemoryTool
from memoryking.sdk import LocalClient


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-vault-")
        self.root = Path(self.temp.name)
        self.credential = LocalStore.initialize(self.root / "db", self.root / "ctl")
        self.store = LocalStore(self.root / "db", self.root / "ctl")
        self.server = Server(self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = LocalClient(self.server.path, self.credential, "demo")
        self.tool = MemoryTool(self.client)
        self.tool.execute(
            {
                "command": "create",
                "path": "/memories/demo/notes/acme.md",
                "file_text": "Acme prefers email.\n",
            }
        )
        self.tool.execute(
            {
                "command": "str_replace",
                "path": "/memories/demo/notes/acme.md",
                "old_str": "email",
                "new_str": "chat",
            }
        )
        self.tool.execute(
            {
                "command": "create",
                "path": "/memories/demo/git/c1.md",
                "file_text": "Switched Acme to chat.",
            }
        )
        self.tool.execute(
            {
                "command": "create",
                "path": "/memories/demo/claims/acme-contact.md",
                "file_text": "---\nevidence: [notes/acme]\n---\nContact Acme by chat.",
            }
        )
        self.vault = self.root / "vault"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.store.close()
        self.temp.cleanup()

    def test_export_writes_newest_versions_with_provenance_and_import_round_trips(self):
        report = vault.export(self.vault, self.client)
        self.assertEqual((report["observations"], report["versions"], report["claims"]), (2, 3, 1))
        self.assertEqual(report["files"], ["git/c1.md", "notes/acme.md", "claims/acme-contact.md"])
        acme = (self.vault / "demo/notes/acme.md").read_text()
        self.assertTrue(acme.startswith("---\nmemoryking: observation\n"))
        self.assertIn("\nversion: 2\n", acme)
        self.assertTrue(acme.endswith("---\nAcme prefers chat.\n"))
        claim = (self.vault / "demo/claims/acme-contact.md").read_text()
        self.assertIn("\nstate: proposed\n", claim)
        self.assertTrue(claim.endswith("---\nContact Acme by chat."))
        self.assertIn("Exported ", (self.vault / "demo/README.md").read_text())
        self.assertEqual(oct((self.vault / "demo").stat().st_mode & 0o777), "0o700")
        # Unchanged files import as nothing; an edited file is a new version; a new file
        # is a new observation; a claim file is a proposal with the evidence it names.
        again = vault.import_(self.vault, self.client)
        self.assertEqual(
            (again["captured"], again["proposed"], again["unchanged"], again["errors"]),
            ([], [], 3, []),
        )
        path = self.vault / "demo/notes/acme.md"
        path.write_text(acme.replace("Acme prefers chat.", "Acme prefers chat, mornings only."))
        (self.vault / "demo/notes/pat.md").write_text("Pat is the contact.\n")
        (self.vault / "demo/claims/pat-contact.md").write_text(
            "---\nevidence: [notes/pat, git/c1]\n---\nAsk for Pat.\n"
        )
        (self.vault / "demo/notes/bad name.md").write_text("ignored")
        dry = vault.import_(self.vault, self.client, dry_run=True)
        self.assertEqual([c["file"] for c in dry["captured"]], ["notes/acme.md", "notes/pat.md"])
        # A dry run writes nothing, so the claim's evidence `notes/pat` cannot resolve yet.
        self.assertEqual(dry["proposed"], [])
        self.assertEqual(
            [e["file"] for e in dry["errors"]], ["notes/bad name.md", "claims/pat-contact.md"]
        )
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM observations").fetchone()[0], 3
        )
        done = vault.import_(self.vault, self.client)
        self.assertEqual(
            [(c["file"], c["version"]) for c in done["captured"]],
            [("notes/acme.md", 3), ("notes/pat.md", 1)],
        )
        self.assertEqual([p["key"] for p in done["proposed"]], ["pat-contact"])
        self.assertEqual(done["unchanged"], 2)
        self.assertEqual([e["file"] for e in done["errors"]], ["notes/bad name.md"])
        again = vault.import_(self.vault, self.client)
        self.assertEqual((again["captured"], again["proposed"], again["unchanged"]), ([], [], 5))
        shown = self.tool.execute({"command": "view", "path": "/memories/demo/notes/acme.md"})
        self.assertIn("\tversion: 3", shown)
        self.assertTrue(shown.endswith("\tAcme prefers chat, mornings only."))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM revisions").fetchone()[0], 2)
        # A second export of the same store reflects every change; nothing was deleted.
        report = vault.export(self.vault, self.client)
        self.assertEqual((report["observations"], report["versions"], report["claims"]), (3, 5, 2))

    def test_cli_exports_and_imports_with_profile_free_paths(self):
        env = {**os.environ, "MEMORYKING_PROJECT": "demo"}
        common = ["--socket", str(self.server.path), "--credential", str(self.credential)]
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "memoryking",
                "vault",
                "export",
                "--dir",
                str(self.vault),
                *common,
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("Exported 2 file(s) from 3 observation(s) and 1 claim(s)", run.stdout)
        (self.vault / "demo/notes/new.md").write_text("A new note.\n")
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "memoryking",
                "vault",
                "import",
                "--dir",
                str(self.vault),
                "--json",
                *common,
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual([c["file"] for c in report["captured"]], ["notes/new.md"])
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "memoryking",
                "vault",
                "import",
                "--dir",
                str(self.vault),
                *common,
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(run.stdout.strip(), "0 captured, 0 proposed, 4 unchanged, 0 skipped")
