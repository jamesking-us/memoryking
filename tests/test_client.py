"""The typed client methods and their async twins, against a real daemon socket."""

import asyncio
import os
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from memoryking import lean
from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
from memoryking.client import AsyncLocalClient, MemoryKing
from memoryking.domain.local import LocalError
from memoryking.memory_api import Briefing, HandOff, Progress, Remembered, Resumed
from memoryking.sdk import LocalClient


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-cl-")
        root = Path(self.temp.name)
        self.credential = LocalStore.initialize(root / "data", root / "control")
        self.store = LocalStore(root / "data", root / "control")
        self.server = Server(self.store)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.client = LocalClient(self.server.path, self.credential, "p")

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.store.close()
        self.temp.cleanup()

    def fails(self, code, call):
        with self.assertRaises(LocalError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_methods_return_typed_results_and_share_the_lean_code_path(self):
        saved = self.client.remember("retry backoff exceeded the deadline", source="pytest")
        self.assertIsInstance(saved, Remembered)
        self.assertFalse(saved.held)
        again = lean.call(
            self.client,
            "remember",
            {"text": "retry backoff exceeded the deadline", "source": "pytest"},
        )
        self.assertEqual(saved.raw, again)
        self.assertEqual(
            self.client.remember("retry backoff exceeded the deadline", source="pytest"), saved
        )

        brief = self.client.recall("deadline")
        self.assertIsInstance(brief, Briefing)
        self.assertIn("retry backoff exceeded the deadline", brief.text)
        self.assertEqual(brief.sources[0]["id"], saved.observation)
        self.assertEqual(brief.raw["format"], "text")
        as_json = self.client.recall("deadline", format="json", as_of=date(2999, 1, 1))
        self.assertEqual(as_json.text, "")
        self.assertIn("guidance", as_json.raw)

        progress = self.client.save_progress(
            "t1",
            "Fix the retry deadline",
            "Investigating",
            next_actions=["Run the regression"],
            failed_attempts=[{"action": "Raise every timeout", "outcome": "Still failed"}],
            share_with=["reviewer"],
        )
        self.assertIsInstance(progress, Progress)
        self.assertEqual((progress.revision, progress.replayed), (1, False))
        later = self.client.save_progress("t1", "Fix the retry deadline", "Fix drafted")
        self.assertEqual(later.revision, 2)
        note = self.client.resume("t1")
        self.assertIsInstance(note, Resumed)
        self.assertIn("Fix drafted", note.text)
        self.assertIn("Run the regression", note.text)  # carried over from the first save
        same = lean.call(self.client, "resume", {"task": "t1"})["text"]
        self.assertEqual(note.text.split("\n\n", 2)[2], same.split("\n\n", 2)[2])

        self.store.grant(self.credential.read_text().strip(), "reviewer", "p", ["read"])
        handed = self.client.hand_off("t1", "reviewer")
        self.assertIsInstance(handed, HandOff)
        self.assertEqual(handed.status, "prepared")
        self.assertTrue(handed.handle)

    def test_bad_arguments_are_refused_before_any_request_with_a_hint(self):
        error = self.fails("invalid_tool_arguments", lambda: self.client.recall(""))
        self.assertTrue(error.hint)
        self.fails("invalid_tool_arguments", lambda: self.client.remember("x", source="a b"))
        self.fails("invalid_tool_arguments", lambda: self.client.resume("t", budget=1))
        self.fails(
            "checkpoint_unavailable", lambda: self.client.hand_off("never-saved", "reviewer")
        )

    def test_context_manager_and_connect_from_environment(self):
        environment = {
            "MEMORYKING_SOCKET": str(self.server.path),
            "MEMORYKING_CREDENTIAL": str(self.credential),
            "MEMORYKING_PROJECT": "p",
            "MEMORYKING_PROFILE": "",
            "MEMORYKING_BINDINGS": "environment=prod",
        }
        with patch.dict(os.environ, environment):
            with MemoryKing.connect() as memory:
                self.assertEqual(memory.project, "p")
                self.assertEqual(memory.bindings, [{"environment": "prod"}])
                memory.remember("connected from the environment")
        for name in ("MEMORYKING_SOCKET", "MEMORYKING_CREDENTIAL", "MEMORYKING_PROJECT"):
            with patch.dict(os.environ, {**environment, name: ""}):
                code = {
                    "MEMORYKING_SOCKET": "socket_required",
                    "MEMORYKING_CREDENTIAL": "credential_required",
                    "MEMORYKING_PROJECT": "project_required",
                }[name]
                self.fails(code, MemoryKing.connect)
        explicit = MemoryKing.connect(
            socket=self.server.path, credential=self.credential, project="p"
        )
        self.assertEqual(explicit.binding, self.client.binding)
        with patch.dict(os.environ, {"MEMORYKING_URL": "https://example.test"}):
            self.fails("credential_required", MemoryKing.connect)

    def test_async_client_matches_the_sync_client(self):
        async def scenario():
            async with await AsyncLocalClient.connect(
                self.server.path, self.credential, "p"
            ) as memory:
                saved = await memory.remember("async finding")
                brief, other = await asyncio.gather(
                    memory.recall("async finding"), memory.recall("nothing relevant here")
                )
                self.assertEqual(brief.sources[0]["id"], saved.observation)
                self.assertIsInstance(other, Briefing)
                progress = await memory.save_progress("a1", "Async task", "Started")
                self.assertEqual(progress.revision, 1)
                self.assertIn("Started", (await memory.resume("a1")).text)
                with self.assertRaises(LocalError) as caught:
                    await memory.recall("")
                self.assertEqual(caught.exception.code, "invalid_tool_arguments")
            return saved

        saved = asyncio.run(scenario())
        self.assertEqual(self.client.remember("async finding"), saved)


if __name__ == "__main__":
    unittest.main()
