"""Anthropic's memory tool over a real local service: six commands, every write receipted."""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from memoryking.adapters.local_files import atomic
from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
from memoryking.memory_tool import DEFINITION, MemoryTool, MemoryToolError, locate, split


class MemoryToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-mt-")
        self.root = Path(self.temp.name)
        self.credential = LocalStore.initialize(self.root / "db", self.root / "ctl")
        self.store = LocalStore(self.root / "db", self.root / "ctl")
        self.server = Server(self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        from memoryking.sdk import LocalClient

        self.owner = self.credential.read_text().strip()
        self.client = LocalClient(self.server.path, self.credential, "demo")
        self.tool = MemoryTool(self.client)

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.store.close()
        self.temp.cleanup()

    def agent(self, name, capabilities):
        """A non-admin principal's tool, bound to the same project."""
        from memoryking.sdk import LocalClient

        token = self.store.grant(self.owner, name, "demo", capabilities)
        path = self.root / f"{name}.credential"
        atomic(path, token.encode())
        return MemoryTool(LocalClient(self.server.path, path, "demo"))

    def run_tool(self, tool=None, **command):
        return (tool or self.tool).execute(command)

    def fails(self, text, tool=None, **command):
        with self.assertRaises(MemoryToolError) as exc:
            self.run_tool(tool, **command)
        self.assertIn(text, exc.exception.message)
        return exc.exception.message

    def observations(self):
        return self.store.db.execute(
            "SELECT source,source_event,principal FROM observations WHERE deleted=0 ORDER BY position"
        ).fetchall()

    def test_paths_stay_under_the_root_and_map_onto_identifiers(self):
        self.assertEqual(DEFINITION, {"type": "memory_20250818", "name": "memory"})
        self.assertEqual(locate("/memories", "demo").kind, "root")
        self.assertEqual(locate("/memories/demo/", "demo").kind, "project")
        self.assertEqual(locate("/memories/demo/README.md", "demo").kind, "readme")
        spot = locate("/memories/demo/Meeting Notes/Acme Corp.md", "demo")
        self.assertEqual((spot.source, spot.name), ("Meeting-Notes", "Acme-Corp"))
        self.assertEqual(spot.path("demo"), "/memories/demo/Meeting-Notes/Acme-Corp.md")
        for bad in (
            "/memories/../etc/passwd",
            "/memories/demo/.hidden/x.md",
            "/etc/passwd",
            "/memoriesX/demo",
            "/memories/demo/a/b/c.md",
            "/memories/demo/notes/x%2e%2e.md",
        ):
            with self.assertRaises(MemoryToolError):
                locate(bad, "demo")
        with self.assertRaises(MemoryToolError):
            locate("/memories/other/notes/x.md", "demo")  # another project does not exist here
        with self.assertRaises(MemoryToolError):
            locate("/memories/demo/notes/plan__v2.md", "demo")  # the version suffix is reserved
        fields, body = split("---\nevidence: [a, b]\nkey: v\n---\nText here\n")
        self.assertEqual((fields, body), ({"evidence": ["a", "b"], "key": "v"}, "Text here\n"))
        self.assertEqual(split("plain"), ({}, "plain"))

    def test_notes_are_versioned_captures_and_views_are_receipted_fetches(self):
        listing = self.run_tool(command="view", path="/memories")
        self.assertTrue(
            listing.startswith("Here're the files and directories up to 2 levels deep in /memories")
        )
        self.assertIn("\t/memories/demo/README.md", listing)
        self.assertIn("\t/memories/demo/claims", listing)
        made = self.run_tool(
            command="create", path="/memories/demo/notes/acme.md", file_text="Acme prefers email.\n"
        )
        self.assertTrue(
            made.startswith("File created successfully at: /memories/demo/notes/acme.md")
        )
        self.assertIn("version 1", made)
        self.assertEqual([tuple(r) for r in self.observations()], [("notes", "acme", "owner")])
        # Re-creating with the same text records nothing; new text is a second version.
        self.assertIn(
            "(unchanged)",
            self.run_tool(
                command="create",
                path="/memories/demo/notes/acme.md",
                file_text="Acme prefers email.",
            ),
        )
        edited = self.run_tool(
            command="str_replace",
            path="/memories/demo/notes/acme.md",
            old_str="email",
            new_str="email follow-ups",
        )
        self.assertTrue(edited.startswith("The memory file has been edited."))
        self.assertIn("version 2", edited)
        self.assertIn("earlier versions are kept", edited)
        self.assertIn("\tAcme prefers email follow-ups.", edited)
        self.assertEqual([r["source_event"] for r in self.observations()], ["acme", "acme__v2"])
        shown = self.run_tool(command="view", path="/memories/demo/notes/acme.md")
        lines = shown.split("\n")
        self.assertEqual(
            lines[0], "Here's the content of /memories/demo/notes/acme.md with line numbers:"
        )
        self.assertEqual(lines[1], "     1\t---")
        self.assertIn("     2\tmemoryking: observation", lines)
        self.assertIn("     7\tversion: 2", lines)
        self.assertEqual(lines[-1], "    14\tAcme prefers email follow-ups.")
        receipts = self.store.db.execute("SELECT count(*) FROM receipts").fetchone()[0]
        self.assertGreaterEqual(receipts, 1)
        ranged = self.run_tool(
            command="view", path="/memories/demo/notes/acme.md", view_range=[14, -1]
        )
        self.assertEqual(ranged.split("\n")[1:], ["    14\tAcme prefers email follow-ups."])
        self.fails(
            "Invalid `view_range`",
            command="view",
            path="/memories/demo/notes/acme.md",
            view_range=[0, 2],
        )
        # Insert counts lines as viewed; the provenance block is not editable.
        inserted = self.run_tool(
            command="insert",
            path="/memories/demo/notes/acme.md",
            insert_line=14,
            insert_text="Contact: Pat.\n",
        )
        self.assertIn("has been edited", inserted)
        self.assertIn("version 3", inserted)
        view = self.run_tool(command="view", path="/memories/demo/notes/acme.md")
        self.assertTrue(
            view.endswith("    14\tAcme prefers email follow-ups.\n    15\tContact: Pat.")
        )
        self.fails(
            "[13, 15]",
            command="insert",
            path="/memories/demo/notes/acme.md",
            insert_line=2,
            insert_text="x",
        )
        self.fails(
            "provenance",
            command="str_replace",
            path="/memories/demo/notes/acme.md",
            old_str="memoryking: observation",
            new_str="x",
        )
        self.fails(
            "did not appear verbatim",
            command="str_replace",
            path="/memories/demo/notes/acme.md",
            old_str="fax",
        )
        self.run_tool(command="create", path="/memories/demo/notes/dup.md", file_text="a b\na b\n")
        self.assertIn(
            "in lines: 14, 15",
            self.fails(
                "Multiple occurrences",
                command="str_replace",
                path="/memories/demo/notes/dup.md",
                old_str="a b",
                new_str="c",
            ),
        )
        self.fails("does not exist", command="view", path="/memories/demo/notes/missing.md")
        self.fails("does not exist", command="view", path="/memories/demo/nowhere")
        # The listing shows sources, files and sizes; the README explains the model.
        project = self.run_tool(command="view", path="/memories/demo")
        self.assertIn("\t/memories/demo/notes/acme.md", project)
        self.assertIn("\t/memories/demo/notes/dup.md", project)
        self.assertIn("K\t/memories/demo/notes", project)
        readme = self.run_tool(command="view", path="/memories/demo/README.md")
        self.assertIn("Sources: notes. Observations: 4. Claims: 0.", readme)
        self.assertIn("evidence", readme)
        # Oversized or empty content is refused before any capture.
        self.fails(
            "needs some text",
            command="create",
            path="/memories/demo/notes/empty.md",
            file_text="  \n",
        )
        self.fails(
            "limited to 65536 bytes",
            command="create",
            path="/memories/demo/notes/big.md",
            file_text="x" * 70000,
        )
        self.fails(
            "README.md is generated",
            command="create",
            path="/memories/demo/README.md",
            file_text="x",
        )
        self.fails("unknown command", command="touch", path="/memories/demo/notes/x.md")
        # A tool_result block carries errors with is_error.
        result = self.tool.tool_result(
            "toolu_1", {"command": "view", "path": "/memories/demo/notes/nope.md"}
        )
        self.assertEqual(result["is_error"], True)
        self.assertEqual(result["tool_use_id"], "toolu_1")
        self.assertNotIn(
            "is_error", self.tool.tool_result("toolu_2", {"command": "view", "path": "/memories"})
        )

    def test_claims_are_proposals_with_evidence_that_wait_for_review(self):
        self.run_tool(
            command="create", path="/memories/demo/notes/acme.md", file_text="Acme prefers email."
        )
        self.run_tool(
            command="create", path="/memories/demo/git/c1.md", file_text="Switched Acme to email."
        )
        self.fails(
            "needs evidence",
            command="create",
            path="/memories/demo/claims/acme-contact.md",
            file_text="Contact Acme by email.",
        )
        self.fails(
            "is not an observation id",
            command="create",
            path="/memories/demo/claims/acme-contact.md",
            file_text="---\nevidence: [notes/nope]\n---\nContact Acme by email.",
        )
        made = self.run_tool(
            command="create",
            path="/memories/demo/claims/acme-contact.md",
            file_text="---\nevidence: [notes/acme, /memories/demo/git/c1.md]\n---\nContact Acme by email.",
        )
        self.assertIn("proposed as revision", made)
        self.assertIn("waits for review", made)
        listed = self.run_tool(command="view", path="/memories/demo/claims")
        self.assertIn("\t/memories/demo/claims/acme-contact.md", listed)
        shown = self.run_tool(command="view", path="/memories/demo/claims/acme-contact.md")
        self.assertIn("\tstate: proposed", shown)
        self.assertIn("\tevidence: [obs_", shown)
        self.assertTrue(shown.endswith("\tContact Acme by email."))
        revision = self.store.db.execute("SELECT id,body FROM revisions").fetchone()
        body = json.loads(revision["body"])
        self.assertEqual(len(body["evidence"]), 2)
        self.assertEqual(body["provenance"]["method"], "memory_file_v1")
        self.assertEqual(body["kind"], "knowledge")
        # A reader sees no proposal; after review the claim is a file for everyone.
        reader = self.agent("reader", ["read"])
        self.assertNotIn(
            "acme-contact", self.run_tool(reader, command="view", path="/memories/demo/claims")
        )
        self.fails(
            "does not exist", reader, command="view", path="/memories/demo/claims/acme-contact.md"
        )
        from memoryking.adapters.knowledge_store import Knowledge

        out = []
        Knowledge(self.store).execute(
            self.owner,
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": "review",
                "key": "r1",
                "args": {
                    "project": "demo",
                    "revision": revision["id"],
                    "decision": "accept",
                    "expected_version": 1,
                    "reason": "reviewed_evidence",
                    "breadth_reviewed": True,
                },
            },
        ).emit(out.append)
        self.assertIn(
            "\tstate: accepted",
            self.run_tool(reader, command="view", path="/memories/demo/claims/acme-contact.md"),
        )
        # Editing an accepted claim proposes a superseding revision with the same evidence.
        edited = self.run_tool(
            command="str_replace",
            path="/memories/demo/claims/acme-contact.md",
            old_str="email",
            new_str="email, never phone",
        )
        self.assertIn("proposed as revision", edited)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM revisions").fetchone()[0], 2)
        self.assertEqual(self.store.db.execute("SELECT version FROM families").fetchone()[0], 3)
        self.fails(
            "retire through review", command="delete", path="/memories/demo/claims/acme-contact.md"
        )
        self.fails(
            "cannot be renamed",
            command="rename",
            old_path="/memories/demo/claims/acme-contact.md",
            new_path="/memories/demo/claims/x.md",
        )

    def test_delete_and_rename_need_an_administrator_and_other_principals_add_versions(self):
        self.run_tool(
            command="create", path="/memories/demo/notes/acme.md", file_text="Acme prefers email."
        )
        writer = self.agent("writer", ["read", "capture"])
        edited = self.run_tool(
            writer,
            command="str_replace",
            path="/memories/demo/notes/acme.md",
            old_str="email",
            new_str="chat",
        )
        self.assertIn("version 2", edited)
        self.assertEqual(
            [tuple(r) for r in self.observations()],
            [("notes", "acme", "owner"), ("notes", "acme__v2", "writer")],
        )
        self.assertIn(
            "\tcaptured_by: writer",
            self.run_tool(command="view", path="/memories/demo/notes/acme.md"),
        )
        self.fails(
            "needs an administrator", writer, command="delete", path="/memories/demo/notes/acme.md"
        )
        self.fails(
            "needs an administrator",
            writer,
            command="rename",
            old_path="/memories/demo/notes/acme.md",
            new_path="/memories/demo/notes/acme-corp.md",
        )
        self.assertEqual(len(self.observations()), 2)
        self.fails("cannot be deleted", command="delete", path="/memories/demo")
        self.fails("delete its files one at a time", command="delete", path="/memories/demo/notes")
        self.fails("does not exist", command="delete", path="/memories/demo/notes/nope.md")
        renamed = self.run_tool(
            command="rename",
            old_path="/memories/demo/notes/acme.md",
            new_path="/memories/demo/customers/acme.md",
        )
        self.assertEqual(
            renamed,
            "Successfully renamed /memories/demo/notes/acme.md to /memories/demo/customers/acme.md",
        )
        self.assertEqual([tuple(r) for r in self.observations()], [("customers", "acme", "owner")])
        self.assertIn(
            "Acme prefers chat.",
            self.run_tool(command="view", path="/memories/demo/customers/acme.md"),
        )
        self.run_tool(command="create", path="/memories/demo/notes/other.md", file_text="x")
        self.fails(
            "already exists",
            command="rename",
            old_path="/memories/demo/customers/acme.md",
            new_path="/memories/demo/notes/other.md",
        )
        self.assertEqual(
            self.run_tool(command="delete", path="/memories/demo/customers/acme.md"),
            "Successfully deleted /memories/demo/customers/acme.md (1 version(s) purged)",
        )
        self.fails("does not exist", command="view", path="/memories/demo/customers/acme.md")
        # A held source still records, and the reply says the note is held.
        from memoryking.adapters.inspection import Inspection

        out = []
        Inspection(self.store).execute(
            self.owner,
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": "trust",
                "key": "t1",
                "args": {"project": "demo", "source": "scraper", "level": "untrusted"},
            },
        ).emit(out.append)
        held = self.run_tool(
            command="create", path="/memories/demo/scraper/page.md", file_text="Scraped."
        )
        self.assertIn("held for review", held)
        self.fails("does not exist", command="view", path="/memories/demo/scraper/page.md")
