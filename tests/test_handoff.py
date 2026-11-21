"""Actual SQLite handoff authorization, complete state, lifecycle and replay contracts."""

import hashlib
import json
import unittest
from pathlib import Path

import test_working as helpers
from migration_helpers import historical_inventory

from memoryking.adapters.handoff import Handoff
from memoryking.adapters.knowledge_store import Knowledge
from memoryking.adapters.local_store import LocalStore
from memoryking.adapters.retrieval import Retrieval
from memoryking.adapters.token_count import encoding
from memoryking.contracts.canonical import ContractError
from memoryking.contracts.validation import validate_handoff


class HandoffTests(unittest.TestCase):
    tearDown = helpers.WorkingTests.tearDown
    scope = helpers.WorkingTests.scope
    command = helpers.WorkingTests.command
    execute = helpers.WorkingTests.execute
    save = helpers.WorkingTests.save
    assign = helpers.WorkingTests.assign
    run_k = helpers.WorkingTests.run_k
    body = helpers.WorkingTests.body
    propose = helpers.WorkingTests.propose
    review = helpers.WorkingTests.review
    constraint = helpers.WorkingTests.constraint
    clock = helpers.WorkingTests.clock
    fails = helpers.WorkingTests.fails
    run_r = helpers.WorkingTests.run_r
    resume = helpers.WorkingTests.resume
    intent = helpers.WorkingTests.intent
    transition = helpers.WorkingTests.transition

    def setUp(self):
        helpers.WorkingTests.setUp(self)
        self.recipient = self.store.grant(self.owner, "reviewer", "p", ["read"])
        self.other = self.store.grant(self.owner, "other", "p", ["read"])
        self.saved = self.save(readers=["owner", "reviewer"])

    def run_h(self, action, args, credential=None, key=None, raw=False):
        self.n += 1
        command = {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": action,
            "key": key or f"h{self.n}",
            "args": args,
        }
        output = []
        Handoff(self.store).execute(credential or self.owner, command).emit(output.append)
        return output[0] if raw else json.loads(output[0])

    def prepare(self, **kw):
        key = kw.pop("key", None)
        credential = kw.pop("credential", None)
        return self.run_h(
            "prepare",
            {
                **self.scope(),
                "checkpoint": self.saved["checkpoint"],
                "recipient": "reviewer",
                "role": "review",
                "stage": "verification",
                "bindings": [{}],
                "ttl_ms": 60000,
                **kw,
            },
            credential=credential,
            key=key,
        )

    def view(self, prepared, preview=False, credential=None, key=None, raw=False, **kw):
        return self.run_h(
            "preview" if preview else "materialize",
            {
                "project": "p",
                "handle": prepared["handle"],
                "max_output_tokens": 6000,
                "tokenizer": "cl100k_base",
                **kw,
            },
            credential or (self.owner if preview else self.recipient),
            key,
            raw,
        )

    def ack(self, prepared, raw, **kw):
        body = json.loads(raw)
        return self.run_h(
            "ack",
            {
                "project": "p",
                "handle": prepared["handle"],
                "materialization": body["handoff"]["materialization"],
                "rendered_hash": hashlib.sha256(raw).hexdigest(),
                **kw,
            },
            self.recipient,
        )

    def replay(self, receipt, credential=None):
        output = []
        Retrieval(self.store).execute(
            credential or self.recipient,
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": "replay",
                "args": {"project": "p", "receipt": receipt},
            },
        ).emit(output.append)
        return output[0]

    def test_prepare_is_opaque_and_does_not_transfer_ownership(self):
        prepared = self.prepare()
        self.assertEqual(prepared["status"], "prepared")
        self.assertNotIn("checkpoint", prepared)
        self.assertNotIn("objective", json.dumps(prepared))
        self.assertNotIn("constraints", json.dumps(prepared))
        self.assertEqual(
            self.store.db.execute("SELECT writer,revision FROM checkpoint_streams").fetchone()[:],
            ("owner", 1),
        )
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM handoff_views").fetchone()[0], 0
        )

    def test_recipient_gets_complete_state_and_ack_does_not_complete_task(self):
        prepared = self.prepare()
        raw = self.view(prepared, raw=True)
        packet = json.loads(raw)
        self.assertEqual(packet["checkpoint"]["state"], helpers.checkpoint())
        self.assertEqual(packet["status"], "ready")
        self.assertEqual(packet["handoff"]["role"], "review")
        self.fails("handoff_ack_invalid", lambda: self.ack(prepared, raw, rendered_hash="0" * 64))
        self.assertEqual(
            packet["usage"]["output_tokens"], len(encoding().encode_ordinary(raw.decode()))
        )
        ack = self.ack(prepared, raw)
        self.assertEqual(ack["status"], "acknowledged")
        self.assertFalse(ack["task_complete"])
        self.assertEqual(self.replay(packet["receipt"]), raw)
        self.assertEqual(self.ack(prepared, raw)["status"], "acknowledged")

    def test_role_name_and_opaque_handle_do_not_authorize_another_identity(self):
        prepared = self.prepare()
        self.fails("handoff_unavailable", lambda: self.view(prepared, credential=self.other))
        self.fails("handoff_unavailable", lambda: self.view(prepared, credential=self.owner))
        self.fails(
            "invalid_request", lambda: self.view(prepared, credential=self.other, role="review")
        )
        self.fails("handoff_unavailable", lambda: self.prepare(recipient="nonexistent"))
        self.fails("handoff_unavailable", lambda: self.prepare(recipient="other"))
        self.fails("denied", lambda: self.prepare(credential=self.recipient))

    def test_checkpoint_audience_and_dependency_closure_must_allow_recipient(self):
        private = self.store.capture(
            self.owner,
            self.ns,
            "private",
            {
                "version": 1,
                "project": "p",
                "source": "test",
                "source_event": "private",
                "text": "restricted evidence",
                "readers": ["owner"],
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )
        body = helpers.checkpoint()
        body["findings"][0]["evidence"] = [{"kind": "observation", "id": private["observation"]}]
        self.saved = self.save(body, revision=1, readers=["owner", "reviewer"])
        self.fails("handoff_unavailable", self.prepare)
        self.saved = self.save(revision=2, readers=["owner"])
        self.fails("handoff_unavailable", self.prepare)

    def test_prepare_carries_the_senders_summary_and_the_recipient_resumes_with_it(self):
        prepared = self.prepare()
        summary = prepared["summary"]
        self.assertEqual(summary["method"], "extractive-v1")
        self.assertTrue(summary["text"])
        replay = self.run_r("replay", {"project": "p", "receipt": summary["receipt"]})
        self.assertEqual(replay["summary"], summary["text"])
        packet = self.resume(credential=self.recipient)
        self.assertEqual(packet["summary"]["text"], summary["text"])
        self.assertEqual(packet["summary"]["author"], "owner")
        self.fails("checkpoint_unavailable", lambda: self.resume(credential=self.other))

    def test_recipient_only_policy_never_leaks_through_preview_or_prepare(self):
        self.recipient = self.store.grant(
            self.owner, "reviewer", "p", ["read", "capture", "policy", "review"]
        )
        policy = self.run_k(
            "propose",
            {
                "project": "p",
                "logical_key": "private",
                "expected_version": 0,
                "body": self.body(
                    text="Recipient secret mandatory instruction",
                    kind="constraint",
                    authority="project",
                    rule_key="private",
                    rule_value="required",
                    effect="require",
                    readers=["reviewer"],
                ),
            },
            credential=self.recipient,
        )
        self.review(policy, credential=self.recipient)
        prepared = self.prepare()
        self.assertNotIn("Recipient secret", json.dumps(prepared))
        for budget in [1, 500, 6000]:
            preview = self.view(prepared, preview=True, max_output_tokens=budget)
            self.assertEqual(preview["status"], "blocked")
            self.assertIsNone(preview["checkpoint"])
            self.assertNotIn("Recipient secret", json.dumps(preview))
            self.assertNotIn(policy["revision"], json.dumps(preview))
            self.assertNotIn("minimum_required_tokens", preview)
        packet = self.view(prepared)
        self.assertIn("Recipient secret", packet["constraints"][0]["text"])

    def test_hidden_recipient_policy_blocks_complete_delivery(self):
        self.constraint("Sender-only essential rule", readers=["owner"])
        prepared = self.prepare()
        raw = self.view(prepared, raw=True)
        packet = json.loads(raw)
        self.assertEqual(packet["status"], "blocked")
        self.assertIsNone(packet["checkpoint"])
        self.assertNotIn("Sender-only", raw.decode())
        self.fails("handoff_ack_invalid", lambda: self.ack(prepared, raw))

    def test_full_preview_uses_intersection_and_is_not_acknowledgeable(self):
        self.constraint("Shared compulsory rule")
        prepared = self.prepare()
        raw = self.view(prepared, preview=True, raw=True)
        self.assertEqual(json.loads(raw)["checkpoint"]["state"], helpers.checkpoint())
        self.assertEqual(json.loads(raw)["handoff"]["mode"], "preview")
        self.assertEqual(json.loads(raw)["context_binding"]["principal"], "owner")
        self.fails("handoff_ack_invalid", lambda: self.ack(prepared, raw))
        self.assertEqual(self.replay(json.loads(raw)["receipt"], self.owner), raw)

    def test_exact_duplicate_materialization_is_released_once_per_request_but_not_recompiled(self):
        prepared = self.prepare(key="prepare")
        again = self.prepare(key="prepare")
        self.assertEqual(again["handle"], prepared["handle"])
        self.assertTrue(again["replayed"])
        raw = self.view(prepared, key="delivery", raw=True)
        self.assertEqual(self.view(prepared, key="delivery", raw=True), raw)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM handoff_views").fetchone()[0], 1
        )
        self.fails(
            "idempotency_conflict",
            lambda: self.view(prepared, key="delivery", max_output_tokens=5000),
        )

    def test_permissions_rechecked_before_duplicate_delivery_and_ack(self):
        prepared = self.prepare()
        raw = self.view(prepared, key="same", raw=True)
        self.store.revoke(self.owner, "reviewer", "p")
        self.fails("denied", lambda: self.view(prepared, key="same"))
        self.fails("denied", lambda: self.ack(prepared, raw))
        self.recipient = self.store.grant(self.owner, "reviewer", "p", ["read"])
        self.fails("handoff_view_stale", lambda: self.view(prepared, key="same"))
        self.assertEqual(self.view(prepared)["status"], "ready")
        sender = self.store.grant(self.owner, "sender", "p", ["read", "handoff"])
        self.saved = self.save(revision=1, readers=["owner", "sender", "reviewer"])
        newer = self.prepare(credential=sender)
        self.store.revoke(self.owner, "sender", "p")
        self.fails("handoff_unavailable", lambda: self.view(newer))

    def test_time_activated_policy_invalidates_old_view_without_epoch_change(self):
        clock = self.clock()
        self.constraint(
            "Newly active condition", valid_start=clock.utc + 1000, valid_end=clock.utc + 10000
        )
        prepared = self.prepare()
        raw = self.view(prepared, key="same", raw=True)
        self.assertEqual(json.loads(raw)["constraints"], [])
        epoch = self.store.db.execute("SELECT policy_epoch FROM tenants").fetchone()[0]
        clock.advance(1001)
        self.assertEqual(
            self.store.db.execute("SELECT policy_epoch FROM tenants").fetchone()[0], epoch
        )
        self.fails("handoff_view_stale", lambda: self.view(prepared, key="same"))
        self.fails("handoff_view_stale", lambda: self.ack(prepared, raw))
        self.assertEqual(len(self.view(prepared)["constraints"]), 1)

    def test_attestation_expiry_invalidates_old_materialization(self):
        clock = self.clock()
        policy = self.constraint()
        self.run_k(
            "attest",
            {
                "project": "p",
                "revision": policy["revision"],
                "expires_ms": clock.utc + 1000,
                "statement": "Checked this condition",
            },
        )
        prepared = self.prepare()
        raw = self.view(prepared, raw=True)
        self.assertTrue(json.loads(raw)["constraints"][0]["attestations"])
        clock.advance(1001)
        self.fails("handoff_view_stale", lambda: self.replay(json.loads(raw)["receipt"]))
        self.assertEqual(self.view(prepared)["constraints"][0]["attestations"], [])

    def test_new_checkpoint_writer_or_preparation_supersedes_old_handle(self):
        prepared = self.prepare()
        replacement = self.prepare(role="implementer", stage="followup")
        self.fails("handoff_unavailable", lambda: self.view(prepared))
        self.saved = self.save(revision=1, readers=["owner", "reviewer"])
        self.fails("handoff_superseded", lambda: self.view(replacement))
        current = self.prepare()
        self.store.grant(self.owner, "worker", "p", ["read", "checkpoint"])
        self.assign(revision=2)
        self.fails("handoff_superseded", lambda: self.view(current))

    def test_cancel_blocks_replay_and_retries_without_erasure_claim(self):
        prepared = self.prepare()
        raw = self.view(prepared, key="delivery", raw=True)
        result = self.run_h("cancel", {"project": "p", "handle": prepared["handle"]}, key="cancel")
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(
            self.run_h("cancel", {"project": "p", "handle": prepared["handle"]}, key="cancel")[
                "replayed"
            ]
        )
        self.fails("handoff_unavailable", lambda: self.view(prepared, key="delivery"))
        self.fails("handoff_unavailable", lambda: self.replay(json.loads(raw)["receipt"]))
        self.assertEqual(json.loads(raw)["checkpoint"]["state"], helpers.checkpoint())

    def test_handoff_expiry_is_half_open_and_auth_instant_allows_bounded_finish(self):
        clock = self.clock()
        prepared = self.prepare(ttl_ms=1000)
        self.store.fault = lambda point: (
            clock.advance(1001) if point == "after_validation" else None
        )
        packet = self.view(prepared)
        self.assertEqual(packet["status"], "ready")
        self.assertLess(packet["evaluated_ms"], prepared["expires_ms"])
        self.store.fault = lambda point: None
        self.fails("handoff_expired", lambda: self.view(prepared))
        self.fails("handoff_expired", lambda: self.replay(packet["receipt"]))

    def test_release_deadline_rolls_back_materialization(self):
        prepared = self.prepare()
        clock = self.clock()
        self.store.fault = lambda point: (
            clock.advance(5001) if point == "after_validation" else None
        )
        self.fails("release_expired", lambda: self.view(prepared))
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM handoff_views").fetchone()[0], 0
        )

    def test_budget_failure_never_silently_drops_essential_state(self):
        prepared = self.prepare()
        raw = self.view(prepared, raw=True, max_output_tokens=100)
        packet = json.loads(raw)
        self.assertEqual(packet["status"], "budget_insufficient")
        self.assertIsNone(packet["checkpoint"])
        self.assertGreater(packet["minimum_required_tokens"], 100)
        self.fails("handoff_ack_invalid", lambda: self.ack(prepared, raw))

    def test_missing_artifact_invalidates_prior_view_and_new_packet_reports_gap(self):
        body = helpers.checkpoint()
        body["repository"].update(
            dirty=True,
            artifacts=[
                {
                    "path": "dirty.patch",
                    "availability": "stored",
                    "observation": self.obs["observation"],
                    "content_hash": self.span["content_hash"],
                    "required": True,
                }
            ],
        )
        self.saved = self.save(body, revision=1, readers=["owner", "reviewer"])
        prepared = self.prepare()
        raw = self.view(prepared, raw=True, key="file")
        self.assertEqual(json.loads(raw)["status"], "ready")
        (self.data / "blobs" / self.tenant / self.span["content_hash"]).unlink()
        self.fails("handoff_view_stale", lambda: self.view(prepared, key="file"))
        fresh = self.view(prepared, raw=True)
        self.assertEqual(json.loads(fresh)["status"], "incomplete")
        self.assertFalse(self.ack(prepared, fresh)["task_complete"])

    def test_operation_result_changes_require_fresh_materialization(self):
        self.intent()
        self.transition("dispatching", 1)
        prepared = self.prepare()
        raw = self.view(prepared, raw=True, key="op")
        self.assertEqual(json.loads(raw)["status"], "incomplete")
        self.transition("succeeded", 2)
        self.fails("handoff_view_stale", lambda: self.ack(prepared, raw))
        self.assertEqual(self.view(prepared)["status"], "ready")

    def test_purge_erases_handoff_copies_and_restore_retires_old_history(self):
        # Purge follows recorded dependencies: this handoff's checkpoint cites the source.
        body = helpers.checkpoint()
        body["references"] = [{"kind": "observation", "id": self.obs["observation"]}]
        self.saved = self.save(body, revision=1, readers=["owner", "reviewer"])
        prepared = self.prepare()
        raw = self.view(prepared, raw=True)
        backup = self.root / "backup"
        self.store.backup(backup)
        self.store.purge(self.owner, self.obs["observation"])
        self.assertEqual(
            [r[0] for r in self.store.db.execute("SELECT state FROM handoffs")], ["purged"]
        )
        self.fails("handoff_unavailable", lambda: self.view(prepared))
        self.fails("replay_purged", lambda: self.replay(json.loads(raw)["receipt"]))
        self.store.close()
        self.owner = LocalStore.restore(self.data, self.control, backup).read_text()
        self.store = LocalStore(self.data, self.control)
        self.k = Knowledge(self.store)
        self.recipient = self.store.grant(self.owner, "reviewer", "p", ["read"])
        self.fails("history_changed", lambda: self.view(prepared))

    def test_closed_shared_handoff_vectors(self):
        vectors = json.loads(
            (
                Path(__file__).resolve().parents[1] / "fixtures/contracts/handoff-vectors.json"
            ).read_text()
        )
        for vector in vectors:
            with self.subTest(vector=vector["name"]):
                if vector["valid"]:
                    validate_handoff(vector["request"])
                else:
                    with self.assertRaises(ContractError):
                        validate_handoff(vector["request"])
        bad = {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": "prepare",
            "key": "bad",
            "args": {},
        }
        self.fails("invalid_request", lambda: Handoff(self.store).execute(self.owner, bad))

    def test_schema_four_upgrade_preserves_checkpoint_and_adds_handoff(self):
        import sqlite3

        self.store.close()
        with sqlite3.connect(self.data / "store.sqlite") as db:
            db.execute("DROP TABLE handoff_views")
            db.execute("DROP TABLE handoffs")
            historical_inventory(db, 4)
        self.store = LocalStore(self.data, self.control)
        self.k = Knowledge(self.store)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 14)
        self.assertEqual(self.view(self.prepare())["checkpoint"]["id"], self.saved["checkpoint"])
