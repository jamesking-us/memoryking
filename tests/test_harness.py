"""Real reference-harness persistence, process interruption and bounded mutation safety."""

import importlib.util
import json
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_knowledge as knowledge_helpers
import test_retrieval as retrieval_helpers
from test_working import checkpoint

from memoryking.adapters.local_files import atomic
from memoryking.adapters.local_ipc import Server
from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import LocalError
from memoryking.harness import LocalFileTool, ReferenceHarness
from memoryking.harness_context import LocalRecordingProvider
from memoryking.sdk import LocalClient

SPEC = importlib.util.spec_from_file_location(
    "harness_demo", Path(__file__).resolve().parents[1] / "tools/harness_demo.py"
)
demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(demo)


class HarnessTests(unittest.TestCase):
    fails = retrieval_helpers.RetrievalTests.fails

    def setUp(self):
        knowledge_helpers.KnowledgeTests.setUp(self)
        self.server = Server(self.store)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.client = LocalClient(self.server.path, self.control / "owner.credential", "p")
        self.tool = LocalFileTool(self.root / "work")
        self.instances = []

    def tearDown(self):
        for harness in self.instances:
            harness.close(checkpoint=False)
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        knowledge_helpers.KnowledgeTests.tearDown(self)

    def start(self, directory="journal", task="task", state=True, **kw):
        harness = ReferenceHarness(
            self.client,
            self.root / directory,
            self.tool,
            task=task,
            state=checkpoint() if state else None,
            **kw,
        )
        self.instances.append(harness)
        return harness

    def wait_for(self, condition):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.02)
        self.fail("Expected harness observation did not arrive")

    def test_accepted_constraints_and_turns_are_durable_before_next_action(self):
        h = self.start()
        h.accept_constraint("Do not overwrite artifacts", approved=True)
        self.assertIn(
            "Do not overwrite artifacts",
            json.loads((self.root / "journal/task.json").read_text())["payload"]["state"][
                "hard_constraints"
            ],
        )
        self.assertEqual(
            h.create_file("create1", "one.json", "actual artifact", approved=True)["state"],
            "succeeded",
        )
        result = h.model_turn(LocalRecordingProvider(self.root / "provider"))
        self.assertIn("Do not overwrite artifacts", Path(result["request_path"]).read_text())
        self.assertEqual(h.resume()["operations"][0]["state"], "succeeded")
        before = h.status()["revision"]
        h.close()
        recovered = self.start(state=False)
        self.assertGreater(recovered.status()["revision"], before)
        self.assertIn("Do not overwrite artifacts", recovered.state()["hard_constraints"])

    def test_periodic_capture_flushes_staged_state_and_getter_is_not_mutable(self):
        h = self.start(flush_seconds=0.05)
        detached = h.state()
        detached["objective"] = "A staged objective"
        self.assertNotEqual(h.state()["objective"], detached["objective"])
        revision = h.status()["revision"]
        h.update_state(detached, defer=True)
        self.wait_for(lambda: h.status()["revision"] > revision)
        self.assertFalse(h.status()["staged"])
        self.assertEqual(h.resume()["checkpoint"]["state"]["objective"], "A staged objective")

    def test_heartbeat_does_not_change_checkpoint_or_progress(self):
        h = self.start()
        before = h.status()
        h.heartbeat()
        h.heartbeat()
        self.assertEqual(h.status()["revision"], before["revision"])
        self.assertEqual(h.status()["checkpoint"], before["checkpoint"])
        self.assertEqual(
            self.store.db.execute(
                "SELECT count(*) FROM observations WHERE source='reference-heartbeat-v1'"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(h.state()["acceptance"], checkpoint()["acceptance"])

    def test_capture_failure_is_visible_and_prevents_mutation_until_reconciled(self):
        h = self.start(flush_seconds=0.05)
        state = h.state()
        state["current_state"] = "Changed but capture temporarily unavailable"
        with patch.object(self.client, "capture", side_effect=LocalError("transport_unavailable")):
            h.update_state(state, defer=True)
            self.wait_for(lambda: h.status()["capture"] == "degraded")
            self.fails(
                "harness_capture_degraded",
                lambda: h.create_file("blocked", "blocked.json", "no", approved=True),
            )
            self.assertFalse((self.tool.root / "blocked.json").exists())
        h.reconcile()
        self.assertEqual(h.status()["capture"], "current")
        self.assertEqual(h.resume()["checkpoint"]["state"]["current_state"], state["current_state"])

    def test_actual_kill_after_checkpoint_commit_retries_without_duplicate_save(self):
        root = self.root / "child"
        demo.kill_child(
            self.server.path,
            self.control / "owner.credential",
            root,
            "ack",
            "constraint",
            "failed",
            "p",
        )
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM checkpoints WHERE task='ack'").fetchone()[
                0
            ],
            1,
        )
        h = ReferenceHarness(
            self.client, root / "journal", LocalFileTool(root / "work"), task="ack"
        )
        self.instances.append(h)
        self.assertEqual(h.status()["revision"], 1)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM checkpoints WHERE task='ack'").fetchone()[
                0
            ],
            1,
        )
        self.assertIsNone(h.data["pending"])

    def test_actual_kill_after_external_effect_recovers_without_rewrite(self):
        root = self.root / "child"
        demo.kill_child(
            self.server.path,
            self.control / "owner.credential",
            root,
            "effect",
            "constraint",
            "failed",
            "p",
        )
        path = root / "work/result.json"
        before = (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
        h = ReferenceHarness(
            self.client, root / "journal", LocalFileTool(root / "work"), task="effect"
        )
        self.instances.append(h)
        self.assertEqual(h.resume()["operations"][0]["state"], "succeeded")
        self.fails(
            "operation_already_recorded",
            lambda: h.create_file("file-operation", "result.json", "same", approved=True),
        )
        self.assertEqual(before, (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns))

    def test_mismatched_readback_keeps_unknown_and_allows_inspection_not_redispatch(self):
        root = self.root / "child"
        demo.kill_child(
            self.server.path,
            self.control / "owner.credential",
            root,
            "effect",
            "constraint",
            "failed",
            "p",
        )
        target = root / "work/result.json"
        atomic(target, b"changed outside the harness")
        h = ReferenceHarness(
            self.client, root / "journal", LocalFileTool(root / "work"), task="effect"
        )
        self.instances.append(h)
        self.assertEqual(h.reconcile()["state"], "unknown")
        self.assertEqual(h.resume()["status"], "incomplete")
        self.fails(
            "harness_recovery_incomplete",
            lambda: h.create_file("another", "another.json", "no", approved=True),
        )
        self.assertEqual(target.read_bytes(), b"changed outside the harness")
        self.assertFalse((root / "work/another.json").exists())

    def test_missing_dirty_artifact_blocks_dependent_work_and_does_not_hide_gap(self):
        h = self.start()
        state = h.state()
        state["repository"].update(dirty=True, artifacts=[])
        h.update_state(state)
        self.assertIn("dirty_state_without_artifacts", h.resume()["recovery_gaps"])
        self.fails(
            "harness_recovery_incomplete",
            lambda: h.create_file("no", "no.json", "no", approved=True),
        )

    def test_identity_binding_owner_lock_and_new_task_cannot_silently_replace_state(self):
        h = self.start()
        self.fails("owner_busy_or_unsafe", lambda: self.start(state=False))
        h.close(checkpoint=False)
        self.fails("harness_already_exists", lambda: self.start())
        self.fails("harness_binding_changed", lambda: self.start(task="other", state=False))
        token = self.store.grant(self.owner, "other", "p", ["read", "checkpoint", "capture"])
        credential = self.control / "other.credential"
        atomic(credential, token.encode())
        self.fails(
            "harness_binding_changed",
            lambda: ReferenceHarness(
                LocalClient(self.server.path, credential, "p"),
                self.root / "journal",
                self.tool,
                task="task",
            ),
        )

    def test_fresh_context_reset_keeps_failed_attempts_in_actual_provider_input(self):
        h = self.start()
        before = h.state()
        h.reset_context()
        result = h.model_turn(LocalRecordingProvider(self.root / "provider"))
        request = json.loads(Path(result["request_path"]).read_bytes())
        packet = json.loads(request["messages"][0]["content"])
        self.assertEqual(
            packet["checkpoint"]["state"]["failed_attempts"], before["failed_attempts"]
        )
        self.assertEqual(
            packet["checkpoint"]["state"]["hard_constraints"], before["hard_constraints"]
        )
        self.assertEqual(request["state_handles"], [])

    def test_explicit_authority_safe_paths_and_provenance_cannot_be_omitted(self):
        h = self.start()
        self.fails(
            "host_authorization_required",
            lambda: h.create_file("no", "no.json", "no", approved=False),
        )
        self.fails(
            "tool_path_invalid", lambda: h.create_file("no", "../escape", "no", approved=True)
        )
        state = h.state()
        state["references"] = []
        self.fails("harness_provenance_removed", lambda: h.update_state(state))
        self.assertEqual(h.resume()["operations"], [])

    def test_stale_second_journal_cannot_overwrite_existing_task(self):
        h = self.start()
        self.fails(
            "checkpoint_revision_conflict", lambda: self.start(directory="different-journal")
        )
        self.assertEqual(h.status()["revision"], 1)

    def test_journal_corruption_fails_before_any_mutation(self):
        h = self.start()
        h.close(checkpoint=False)
        path = self.root / "journal/task.json"
        body = json.loads(path.read_text())
        body["payload"]["state"]["objective"] = "Corrupted state"
        atomic(path, canonical_bytes(body))
        self.fails("harness_journal_integrity", lambda: self.start(state=False))

    def test_stored_artifact_is_recoverable_but_missing_blob_is_reported(self):
        h = self.start()
        h.create_file("create1", "artifact.json", "durable dirty content", approved=True)
        artifact = h.store_artifact("artifact.json", approved=True)
        packet = h.resume()
        self.assertEqual(packet["status"], "ready")
        self.assertEqual(packet["checkpoint"]["state"]["repository"]["artifacts"], [artifact])
        row = self.store.db.execute(
            "SELECT blob FROM observations WHERE id=?", (artifact["observation"],)
        ).fetchone()
        (self.store.root / "blobs" / "main" / row["blob"]).unlink()
        self.assertIn("stored_artifact_unavailable", h.resume()["recovery_gaps"])

    def test_prepared_handoff_survives_unchanged_sender_shutdown(self):
        token = self.store.grant(self.owner, "recipient", "p", ["read"])
        credential = self.control / "recipient.credential"
        atomic(credential, token.encode())
        h = self.start(readers=["owner", "recipient"])
        prepared = h.prepare_handoff("recipient", "review", "verify")
        h.heartbeat()
        revision = h.status()["revision"]
        h.close()
        recipient = LocalClient(self.server.path, credential, "p")
        packet = recipient.handoff(
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": "materialize",
                "key": "recipient-view",
                "args": {
                    "project": "p",
                    "handle": prepared["handle"],
                    "max_output_tokens": 6000,
                    "tokenizer": "cl100k_base",
                },
            }
        )
        self.assertEqual(packet["checkpoint"]["revision"], revision)
        self.assertEqual(packet["status"], "ready")

    def test_failed_model_turn_is_captured_without_reusing_raw_derived_output(self):
        h = self.start()
        provider = LocalRecordingProvider(self.root / "provider")
        with patch.object(
            provider, "dispatch", side_effect=LocalError("provider_handle_unavailable")
        ):
            self.fails("provider_handle_unavailable", lambda: h.model_turn(provider))
        self.assertIsNone(h.status()["activity"])
        packet = h.resume()
        self.assertEqual(packet["status"], "ready")
        self.assertEqual(h.status()["revision"], 3)

    def test_actual_kill_during_model_turn_records_unknown_without_restoring_provider_state(self):
        root = self.root / "child"
        demo.kill_child(
            self.server.path,
            self.control / "owner.credential",
            root,
            "model",
            "constraint",
            "failed",
            "p",
        )
        self.assertEqual(len(list((root / "provider").glob("*.json"))), 1)
        h = ReferenceHarness(
            self.client, root / "journal", LocalFileTool(root / "work"), task="model"
        )
        self.instances.append(h)
        self.assertIsNone(h.status()["activity"])
        self.assertTrue(any("unobserved result" in item["text"] for item in h.state()["findings"]))
        result = h.model_turn(LocalRecordingProvider(root / "provider"))
        self.assertEqual(json.loads(Path(result["request_path"]).read_bytes())["state_handles"], [])

    def test_tool_error_is_captured_and_existing_file_is_never_overwritten(self):
        h = self.start()
        path = self.tool.root / "existing.json"
        atomic(path, b"existing content")
        self.fails(
            "tool_target_exists",
            lambda: h.create_file("conflicting", "existing.json", "replacement", approved=True),
        )
        self.assertEqual(path.read_bytes(), b"existing content")
        self.assertEqual(h.resume()["operations"][0]["state"], "failed")
        self.assertIsNone(h.status()["pending_operation"])


if __name__ == "__main__":
    unittest.main()
