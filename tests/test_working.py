"""P05 checkpoint and operation recovery tests against actual SQLite and durable files."""

import copy
import json
import os
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import test_knowledge as knowledge_helpers
import test_retrieval as retrieval_helpers
from migration_helpers import historical_inventory

from memoryking.adapters.knowledge_store import Knowledge
from memoryking.adapters.local_ipc import Server, request
from memoryking.adapters.local_store import LocalStore
from memoryking.adapters.retrieval import Retrieval
from memoryking.adapters.token_count import encoding
from memoryking.adapters.working import MUTATIONS, Working
from memoryking.contracts.canonical import ContractError
from memoryking.contracts.validation import validate_working
from memoryking.domain.local import LocalError

ROOT = Path(__file__).resolve().parents[1]


def checkpoint():
    return json.loads((ROOT / "fixtures/contracts/checkpoint.json").read_text())


class WorkingTests(unittest.TestCase):
    setUp = knowledge_helpers.KnowledgeTests.setUp
    tearDown = knowledge_helpers.KnowledgeTests.tearDown
    run_k = knowledge_helpers.KnowledgeTests.run_k
    body = knowledge_helpers.KnowledgeTests.body
    propose = knowledge_helpers.KnowledgeTests.propose
    review = knowledge_helpers.KnowledgeTests.review
    fails = retrieval_helpers.RetrievalTests.fails
    clock = retrieval_helpers.RetrievalTests.clock
    run_r = retrieval_helpers.RetrievalTests.run_r
    constraint = retrieval_helpers.RetrievalTests.constraint

    def scope(self, **kw):
        return {"project": "p", "task": "task", "run": None, **kw}

    def command(self, action, args, key=None):
        self.n += 1
        result = {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": action,
            "args": args,
        }
        if action in MUTATIONS:
            result["key"] = key or f"w{self.n}"
        return result

    def execute(self, command, credential=None, raw=False):
        output = []
        Working(self.store).execute(credential or self.owner, command).emit(output.append)
        return output[0] if raw else json.loads(output[0])

    def save(self, body=None, revision=0, credential=None, key=None, **kw):
        return self.execute(
            self.command(
                "save",
                {
                    **self.scope(),
                    "expected_revision": revision,
                    "readers": ["owner"],
                    "checkpoint": body or checkpoint(),
                    **kw,
                },
                key,
            ),
            credential,
        )

    def resume(self, credential=None, raw=False, **kw):
        return self.execute(
            self.command(
                "resume",
                {
                    **self.scope(),
                    "checkpoint": None,
                    "bindings": [{}],
                    "max_output_tokens": 6000,
                    "tokenizer": "cl100k_base",
                    **kw,
                },
            ),
            credential,
            raw,
        )

    def assign(self, writer="worker", revision=1):
        return self.execute(
            self.command(
                "assign_writer",
                {
                    **self.scope(),
                    "expected_revision": revision,
                    "writer": writer,
                    "reason": "test_reassignment",
                },
            )
        )

    def intent(self, operation="op", **kw):
        return self.execute(
            self.command(
                "operation_intent",
                {
                    **self.scope(),
                    "operation": operation,
                    "target_hash": "a" * 64,
                    "parameters_hash": "b" * 64,
                    "method": "readback",
                    "provider_key": None,
                    **kw,
                },
            )
        )

    def transition(self, state, revision, credential=None, key=None, **kw):
        args = {
            "project": "p",
            "operation": "op",
            "expected_revision": revision,
            "state": state,
            "reconciled": False,
            "evidence": [],
            "result_hash": None,
            **kw,
        }
        if state in {"succeeded", "failed"}:
            args.update(
                evidence=kw.get(
                    "evidence", [{"kind": "observation", "id": self.obs["observation"]}]
                ),
                result_hash=kw.get("result_hash", "c" * 64),
            )
        return self.execute(self.command("operation_transition", args, key), credential)

    def operations(self):
        return self.execute(self.command("operations", self.scope()))["operations"]

    def restart(self):
        self.store.close()
        self.store = LocalStore(self.data, self.control)
        self.k = Knowledge(self.store)

    def summarize(self, credential=None, key=None, **kw):
        return self.execute(
            self.command(
                "summarize",
                {
                    **self.scope(),
                    "checkpoint": None,
                    "source": "extractive",
                    "text": None,
                    "permit": None,
                    "readers": None,
                    **kw,
                },
                key,
            ),
            credential,
        )

    def test_summaries_are_derived_artifacts_shown_first_on_resume(self):
        saved = self.save(readers=["owner", "reader"])
        self.store.grant(self.owner, "reader", "p", ["read"])
        before = self.resume()
        self.assertIsNone(before["summary"])
        self.store.capture(
            self.owner,
            self.ns,
            "later",
            {
                "version": 1,
                "project": "p",
                "source": "pytest",
                "source_event": "later",
                "text": "SECRET later observation text",
                "readers": None,
                "classification": "internal",
                "observed_at_ms": 2,
            },
        )
        result = self.summarize(key="s1")
        self.assertEqual(result["status"], "derived")
        self.assertEqual(result["method"], "extractive-v1")
        self.assertEqual(result["checkpoint"], saved["checkpoint"])
        self.assertIn(
            "1 observation recorded since the checkpoint (from pytest)", result["summary"]
        )
        self.assertNotIn("SECRET", result["summary"])
        kinds = sorted(d["kind"] for d in result["sources"])
        self.assertEqual(kinds, ["checkpoint", "observation"])
        again = self.summarize(key="s1")  # idempotent replay, new receipt and instant
        self.assertTrue(again["replayed"])
        for field in ("summary", "method", "checkpoint", "sources", "readers"):
            self.assertEqual(again[field], result[field])
        replay = self.run_r("replay", {"project": "p", "receipt": result["receipt"]})
        self.assertEqual(replay["schema"], "session-summary-v1")
        self.assertEqual(replay["summary"], result["summary"])
        self.assertEqual(replay["receipt"], result["receipt"])
        after = self.resume()
        self.assertEqual(after["summary"]["text"], result["summary"])
        self.assertEqual(after["summary"]["receipt"], result["receipt"])
        self.assertEqual(after["summary"]["author"], "owner")
        text = self.resume(format="text")["text"]
        self.assertIn("## Where this left off", text)
        self.assertLess(text.index("Where this left off"), text.index("Where it stands"))
        # A reader of the checkpoint sees the summary; a restricted summary is reader-bound.
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        self.assertEqual(self.resume(credential=reader)["summary"]["text"], result["summary"])
        narrow = self.summarize(key="s2", readers=["owner"])
        self.assertEqual(self.resume()["summary"]["receipt"], narrow["receipt"])
        self.assertEqual(self.resume(credential=reader)["summary"]["receipt"], result["receipt"])
        # A provider summary without an audited reply is refused.
        self.fails(
            "summary_source_invalid",
            lambda: self.summarize(
                key="s3", source="provider", text="made up", permit="permit_" + "0" * 32
            ),
        )
        self.fails("summary_source_invalid", lambda: self.summarize(key="s4", source="provider"))
        # A new revision starts without a summary; the old one stays with its checkpoint.
        self.save(revision=1, readers=["owner", "reader"])
        self.assertIsNone(self.resume()["summary"])
        kinds = [
            e["kind"]
            for e in self.store.db.execute(
                "SELECT kind FROM events WHERE subject=?", (saved["checkpoint"],)
            ).fetchall()
        ]
        self.assertIn("summary_derived", kinds)

    def test_idle_summaries_are_derived_once_by_the_owner_between_requests(self):
        clock = self.clock()
        saved = self.save()
        self.store.summary_idle_ms = 1000
        self.assertEqual(self.store.idle_summaries(clock.utc), 0)  # not idle yet
        clock.utc += 2000
        self.assertEqual(self.store.idle_summaries(clock.utc), 1)
        self.assertEqual(self.store.idle_summaries(clock.utc), 0)  # already summarized
        summary = self.resume()["summary"]
        self.assertEqual(summary["author"], "owner")
        self.assertEqual(summary["revision"], saved["revision"])
        self.store.summary_idle_ms = 0
        self.save(revision=1)
        clock.utc += 2000
        self.assertEqual(self.store.idle_summaries(clock.utc), 0)  # disabled
        self.store.summary_idle_ms = 1000
        self.store._last_summary_scan = 0
        self.store.housekeeping()
        self.assertIsNotNone(self.resume()["summary"])
        # A checkpoint the owner may not read is left alone.
        agent = self.store.grant(self.owner, "agent", "p", ["checkpoint", "read"])
        self.save(readers=["agent"], credential=agent, task="private")
        clock.utc += 2000
        self.assertEqual(self.store.idle_summaries(clock.utc), 0)

    def test_full_checkpoint_current_policy_and_exact_budget(self):
        body = checkpoint()
        saved = self.save(body)
        self.constraint("Always preserve rollback instructions")
        raw = self.resume(raw=True)
        packet = json.loads(raw)
        self.assertEqual(packet["status"], "ready")
        self.assertEqual(packet["checkpoint"]["state"], body)
        self.assertEqual(packet["checkpoint"]["id"], saved["checkpoint"])
        self.assertEqual(packet["constraints"][0]["text"], "Always preserve rollback instructions")
        self.assertEqual(
            packet["usage"]["output_tokens"], len(encoding().encode_ordinary(raw.decode()))
        )
        self.assertLessEqual(packet["usage"]["output_tokens"], 6000)
        self.restart()
        self.assertEqual(self.resume()["checkpoint"]["state"], body)

    def test_essential_set_never_truncated_and_capture_gaps_explicit(self):
        body = checkpoint()
        body["hard_constraints"] = ["Unabridged safety instruction " * 200]
        body["capture"]["gaps"] = ["Tool result was not observed"]
        self.save(body)
        small = self.resume(max_output_tokens=400)
        self.assertEqual(small["status"], "budget_insufficient")
        self.assertGreater(small["minimum_required_tokens"], 400)
        self.assertIsNone(small["checkpoint"])
        self.assertFalse(small["usage"]["budget_compliant"])
        full = self.resume()
        self.assertEqual(full["status"], "incomplete")
        self.assertEqual(full["checkpoint"]["state"], body)

    def test_compare_and_swap_run_boundaries_and_writer_reassignment(self):
        self.save()
        self.fails("checkpoint_revision_conflict", lambda: self.save())
        worker = self.store.grant(self.owner, "worker", "p", ["read", "checkpoint"])
        self.fails(
            "checkpoint_writer_denied",
            lambda: self.save(revision=1, credential=worker, readers=["worker"]),
        )
        self.assertEqual(
            self.save(credential=worker, readers=["worker"], run="separate")["revision"], 1
        )
        self.assertEqual(self.assign()["revision"], 2)
        self.fails("checkpoint_writer_denied", lambda: self.save(revision=2))
        self.fails("checkpoint_unavailable", lambda: self.resume(credential=worker))
        self.save(revision=2, credential=worker, readers=["worker"])
        self.assertEqual(self.resume(credential=worker)["checkpoint"]["writer"], "worker")

    def test_competing_cas_writers_have_one_winner(self):
        self.save()
        commands = [
            self.command(
                "save",
                {
                    **self.scope(),
                    "expected_revision": 1,
                    "readers": ["owner"],
                    "checkpoint": checkpoint(),
                },
            )
            for _ in range(2)
        ]

        def run(command):
            try:
                return self.execute(command)["revision"]
            except LocalError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(run, commands))
        self.assertCountEqual(results, [2, "checkpoint_revision_conflict"])

    def test_current_retry_hash_and_grants_before_retry(self):
        worker = self.store.grant(self.owner, "worker", "p", ["read", "checkpoint"])
        args = {
            **self.scope(),
            "expected_revision": 0,
            "readers": ["worker"],
            "checkpoint": checkpoint(),
        }
        command = self.command("save", args, "stable")
        first = self.execute(command, worker)
        retry = self.execute(command, worker)
        self.assertEqual(first["checkpoint"], retry["checkpoint"])
        self.assertFalse(first["replayed"])
        self.assertTrue(retry["replayed"])
        different = copy.deepcopy(command)
        different["args"]["checkpoint"]["objective"] = "Different objective"
        self.fails("idempotency_conflict", lambda: self.execute(different, worker))
        self.store.revoke(self.owner, "worker", "p")
        self.fails("denied", lambda: self.execute(command, worker))

    def replay_result(self, credential, command):
        return Retrieval(self.store).execute(credential, command)

    def test_complete_evidence_closure_and_replay_reauthorization(self):
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        body = checkpoint()
        body["findings"][0]["evidence"] = [{"kind": "observation", "id": self.obs["observation"]}]
        self.save(body, readers=["owner", "reader"])
        packet = self.resume(credential=reader)
        replay = {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": "replay",
            "args": {"project": "p", "receipt": packet["receipt"]},
        }
        self.store.db.execute("UPDATE observations SET readers=?", (json.dumps(["owner"]),))
        self.fails("checkpoint_evidence_unavailable", lambda: self.resume(credential=reader))
        self.fails("replay_denied", lambda: self.replay_result(reader, replay))
        self.store.db.execute("UPDATE observations SET readers='null'")
        self.store.db.execute("UPDATE checkpoints SET readers=?", (json.dumps(["owner"]),))
        self.fails("replay_denied", lambda: self.replay_result(reader, replay))

    def test_purge_erases_checkpoint_copies_and_retires_retries(self):
        body = checkpoint()
        body["references"] = [{"kind": "observation", "id": self.obs["observation"]}]
        saved = self.save(body, key="save")
        packet = self.resume()
        self.store.purge(self.owner, self.obs["observation"])
        self.assertIsNone(
            self.store.db.execute(
                "SELECT body FROM checkpoints WHERE id=?", (saved["checkpoint"],)
            ).fetchone()[0]
        )
        self.assertIsNone(
            self.store.db.execute(
                "SELECT body FROM rendered_artifacts WHERE id=?", (packet["receipt"],)
            ).fetchone()[0]
        )
        self.fails("checkpoint_unavailable", self.resume)

    def test_dirty_artifact_manifest_does_not_claim_durable_bytes(self):
        body = checkpoint()
        body["repository"].update(
            dirty=True,
            artifacts=[
                {
                    "path": "change.patch",
                    "availability": "external",
                    "observation": None,
                    "content_hash": None,
                    "required": True,
                }
            ],
        )
        self.save(body)
        result = self.resume()
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("required_artifact_not_durable", result["recovery_gaps"])
        artifact = body["repository"]["artifacts"][0]
        artifact.update(
            availability="stored",
            observation=self.obs["observation"],
            content_hash=self.span["content_hash"],
        )
        self.save(body, revision=1)
        self.assertEqual(self.resume()["status"], "ready")
        blob = self.store.db.execute("SELECT blob FROM observations").fetchone()[0]
        path = self.data / "blobs" / self.tenant / blob
        self.assertTrue(path.is_file())
        path.unlink()
        self.assertEqual(self.resume()["status"], "incomplete")
        self.fails("artifact_unavailable", lambda: self.save(body, revision=2))

    def test_unsafe_artifact_paths_evidence_free_success_and_duplicates_reject(self):
        for path in ["/tmp/a", "../a", "a//b", "a\\b", "a/./b"]:
            body = checkpoint()
            body["repository"]["artifacts"] = [
                {
                    "path": path,
                    "availability": "missing",
                    "observation": None,
                    "content_hash": None,
                    "required": True,
                }
            ]
            self.fails("artifact_path_invalid", lambda body=body: self.save(body))
        body = checkpoint()
        body["acceptance"][0]["state"] = "passed"
        self.fails("verification_evidence_required", lambda: self.save(body))
        body["acceptance"][0]["state"] = "waived"
        self.fails("waiver_decision_required", lambda: self.save(body))
        body = checkpoint()
        body["acceptance"] *= 2
        self.fails("duplicate_criterion", lambda: self.save(body))

    def test_hidden_policy_blocks_and_expiry_uses_persisted_instant(self):
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        self.save(readers=["owner", "reader"])
        self.constraint("Private compulsory rule", readers=["owner"])
        packet = self.resume(credential=reader)
        self.assertEqual(packet["status"], "blocked")
        self.assertIsNone(packet["checkpoint"])
        self.assertNotIn("Private compulsory", json.dumps(packet))
        clock = self.clock()
        expiring = self.store.grant(self.owner, "expiring", "p", ["read"], expires=clock.utc + 50)
        self.save(revision=1, readers=["owner", "expiring"])
        self.store.fault = lambda point: clock.advance(100) if point == "after_validation" else None
        output = self.resume(credential=expiring)
        self.assertLess(output["evaluated_ms"], clock.utc)
        self.store.fault = lambda point: None
        self.fails("denied", lambda: self.resume(credential=expiring))

    def test_release_deadline_rolls_back_before_commit(self):
        clock = self.clock()
        self.save()
        count = self.store.db.execute("SELECT count(*) FROM receipts").fetchone()[0]
        self.store.fault = lambda point: (
            clock.advance(5001) if point == "after_validation" else None
        )
        self.fails("release_expired", self.resume)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM receipts").fetchone()[0], count
        )

    def test_operation_state_machine_retry_and_no_automatic_redispatch(self):
        self.save()
        epoch = self.store.db.execute("SELECT policy_epoch FROM tenants").fetchone()[0]
        self.intent()
        self.assertEqual(
            self.store.db.execute("SELECT policy_epoch FROM tenants").fetchone()[0], epoch
        )
        first = self.transition("dispatching", 1, key="dispatch")
        retry = self.transition("dispatching", 1, key="dispatch")
        self.assertFalse(first["replayed"])
        self.assertTrue(retry["replayed"])
        self.restart()
        self.assertTrue(self.operations()[0]["reconciliation_required"])
        self.assertEqual(self.resume()["status"], "incomplete")
        self.transition("unknown", 2)
        self.fails("operation_transition_invalid", lambda: self.transition("dispatching", 3))
        self.fails("operation_reconciliation_required", lambda: self.transition("succeeded", 3))
        self.fails(
            "operation_result_evidence_required",
            lambda: self.transition("succeeded", 3, reconciled=True, evidence=[]),
        )
        self.transition("succeeded", 3, reconciled=True)
        self.assertEqual(self.resume()["status"], "ready")
        self.fails("operation_identity_consumed", self.intent)

    def test_stale_writer_cannot_dispatch_and_new_writer_sees_old_ambiguity(self):
        self.save()
        self.intent()
        self.transition("dispatching", 1)
        worker = self.store.grant(self.owner, "worker", "p", ["read", "checkpoint"])
        self.assign()
        self.fails("checkpoint_writer_denied", lambda: self.transition("dispatching", 1))
        body = checkpoint()
        body["operation_ids"] = ["op"]
        self.save(body, revision=2, credential=worker, readers=["worker"])
        result = self.resume(credential=worker)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["operations"][0]["principal"], "owner")

    def test_backup_restore_retains_independent_operation_identity(self):
        self.save()
        backup = self.root / "backup"
        self.store.backup(backup)
        old = self.store.incarnation
        self.intent()
        self.transition("dispatching", 1)
        self.store.close()
        self.owner = LocalStore.restore(self.data, self.control, backup).read_text()
        self.store = LocalStore(self.data, self.control)
        self.k = Knowledge(self.store)
        self.assertNotEqual(self.store.incarnation, old)
        operation = self.operations()[0]
        self.assertEqual(operation["state"], "dispatching")
        self.assertTrue(operation["reconciliation_required"])
        self.fails("history_changed", self.resume)
        self.fails("checkpoint_reconciliation_required", lambda: self.save(revision=1))
        self.assign(writer="owner")
        self.fails("operation_identity_consumed", self.intent)
        self.fails("operation_reconciliation_required", lambda: self.transition("dispatching", 2))
        self.transition("succeeded", 2, reconciled=True)
        self.assertFalse(self.operations()[0]["reconciliation_required"])
        self.save(revision=2)
        self.assertEqual(self.resume()["status"], "ready")

    def test_independent_journal_crash_replays_intent_and_original_retry(self):
        self.save()
        command = self.command(
            "operation_intent",
            {
                **self.scope(),
                "operation": "op",
                "target_hash": "a" * 64,
                "parameters_hash": "b" * 64,
                "method": "manual",
                "provider_key": None,
            },
            "crash",
        )
        self.store.close()
        script = """
import json,os,sys
from pathlib import Path
from memoryking.adapters.local_store import LocalStore
from memoryking.adapters.working import Working
store=LocalStore(Path(sys.argv[1]),Path(sys.argv[2]))
store.fault=lambda point: os._exit(71) if point=='after_journal' else None
Working(store).execute((Path(sys.argv[2])/'owner.credential').read_text(),json.loads(sys.argv[3]))
"""
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.data), str(self.control), json.dumps(command)],
            env={k: v for k, v in os.environ.items() if k not in {"PYTHONHOME", "PYTHONPATH"}},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 71, result.stderr)
        self.store = LocalStore(self.data, self.control)
        self.assertEqual(self.operations()[0]["state"], "intent_recorded")
        self.assertTrue(self.execute(command)["replayed"])

    def test_real_socket_working_request_and_closed_schema(self):
        server = Server(self.store)
        socket = server.path
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            command = self.command(
                "save",
                {
                    **self.scope(),
                    "expected_revision": 0,
                    "readers": ["owner"],
                    "checkpoint": checkpoint(),
                },
            )
            result = request(socket, self.owner, "working", {"command": command})
            self.assertEqual(result["status"], "durable")
            bad = copy.deepcopy(command)
            bad["args"]["checkpoint"]["hidden_chain_of_thought"] = "not accepted"
            self.fails("invalid_request", lambda: self.execute(bad))
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_shared_contract_vectors(self):
        vectors = json.loads((ROOT / "fixtures/contracts/working-vectors.json").read_text())
        for vector in vectors:
            with self.subTest(vector=vector["name"]):
                if vector["valid"]:
                    validate_working(vector["request"])
                else:
                    with self.assertRaises(ContractError):
                        validate_working(vector["request"])

    def test_restore_retains_safe_operation_identity_when_result_source_was_lost(self):
        self.save()
        backup = self.root / "backup"
        self.store.backup(backup)
        self.intent()
        self.transition("dispatching", 1)
        extra = self.store.capture(
            self.owner,
            self.ns,
            "late",
            {
                "version": 1,
                "project": "p",
                "source": "test",
                "source_event": "late",
                "text": "External result observed after backup",
                "readers": ["owner"],
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )
        self.transition(
            "succeeded", 2, evidence=[{"kind": "observation", "id": extra["observation"]}]
        )
        self.store.close()
        self.owner = LocalStore.restore(self.data, self.control, backup).read_text()
        self.store = LocalStore(self.data, self.control)
        operation = self.operations()[0]
        self.assertEqual(operation["state"], "succeeded")
        self.assertFalse(operation["evidence_available"])
        self.assertEqual(operation["evidence"], [])
        self.assertIsNone(operation["result_hash"])
        self.assertTrue(operation["reconciliation_required"])
        self.fails("operation_reconciliation_required", lambda: self.transition("succeeded", 3))
        self.transition("succeeded", 3, reconciled=True)
        self.assertFalse(self.operations()[0]["reconciliation_required"])

    def test_schema_three_migrates_without_rewriting_existing_observations(self):
        import sqlite3

        self.store.close()
        with sqlite3.connect(self.data / "store.sqlite") as db:
            for table in ["checkpoint_streams", "checkpoints", "operation_intents"]:
                db.execute("DROP TABLE " + table)
            historical_inventory(db, 3)
        self.store = LocalStore(self.data, self.control)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 11)
        self.assertEqual(
            self.store.db.execute("SELECT id FROM observations").fetchone()[0],
            self.obs["observation"],
        )
        self.save()
        self.assertEqual(self.resume()["status"], "ready")

    def test_text_resume_reads_as_a_handover_note(self):
        body = checkpoint()
        body["failed_attempts"] = [
            {
                "action": "Increase every timeout",
                "outcome": "Exceeded the deadline",
                "do_not_repeat_when": "the total deadline is unchanged",
            }
        ]
        saved = self.save(body)
        packet = self.resume(format="text")
        self.assertEqual(packet["format"], "text")
        text = packet["text"]
        self.assertTrue(text.startswith("# Resume: " + body["objective"]))
        self.assertIn("## Where it stands", text)
        self.assertIn(body["current_state"], text)
        self.assertIn("## Next actions", text)
        self.assertIn("1. " + body["next_actions"][0], text)
        self.assertIn("## Do not repeat", text)
        self.assertIn("Increase every timeout", text)
        self.assertIn("## Blockers", text)
        self.assertIn(saved["checkpoint"], text)
        self.assertNotIn("checkpoint", {k for k in packet if k != "receipt"})

    def test_capacity_rejects_before_a_stream_becomes_unresumable(self):
        self.save()
        for number in range(32):
            self.intent(operation=f"op{number}")
        self.fails("operation_scope_capacity", lambda: self.intent(operation="overflow"))
        self.assertEqual(len(self.resume(max_output_tokens=16000)["operations"]), 32)
        self.assertEqual(
            self.store.status(self.owner, "p")["capacity"]["operation_intents"]["used"], 32
        )
        # Resolving an intent releases its slot while its identity stays consumed.
        self.transition("dispatching", 1, operation="op0")
        self.transition("succeeded", 2, operation="op0")
        self.intent(operation="overflow")
        self.fails("operation_identity_consumed", lambda: self.intent(operation="op0"))
        self.assertEqual(
            self.store.status(self.owner, "p")["capacity"]["operation_intents"]["used"], 32
        )

    def test_unacknowledged_dispatch_is_recovered_as_ambiguous_after_deadline(self):
        self.save()
        self.intent()
        clock = self.clock()
        self.store.fault = lambda point: (
            clock.advance(5001) if point == "after_validation" else None
        )
        self.fails("release_expired", lambda: self.transition("dispatching", 1))
        self.fails("recovery_required", self.operations)
        self.store.close()
        self.store = LocalStore(self.data, self.control, clock=clock)
        self.assertTrue(self.operations()[0]["reconciliation_required"])
        self.assertEqual(self.operations()[0]["state"], "dispatching")

    def test_expired_retained_resumes_are_collected_without_losing_checkpoint(self):
        from memoryking.adapters.local_store import RETRY_MS

        self.save()
        old = self.resume()
        clock = self.clock()
        clock.advance(RETRY_MS)
        self.assertEqual(self.resume()["status"], "ready")
        row = self.store.db.execute(
            "SELECT state,body FROM rendered_artifacts WHERE id=?", (old["receipt"],)
        ).fetchone()
        self.assertEqual(tuple(row), ("expired", None))
