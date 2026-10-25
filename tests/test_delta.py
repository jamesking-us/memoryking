"""Canonical transport reconstruction and current-authorized full fallback."""

import copy
import json
import unittest

import test_knowledge as knowledge
import test_retrieval as retrieval
import test_working as working

from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.delta import SCHEMA, apply, changes, materialize
from memoryking.domain.local import LocalError


class DeltaDomainTests(unittest.TestCase):
    def test_typed_ordered_changes_preserve_arrays_and_reject_ambiguous_paths(self):
        before = {"a": {"negation": "not safe", "obsolete": False}, "list": [1, 2]}
        after = {"a": {"negation": "not safe", "new": True}, "list": [2, 1]}
        patch = changes(before, after)
        self.assertEqual(apply(before, patch), after)
        self.assertEqual(before["list"], [1, 2])
        self.assertEqual(
            canonical_bytes(apply({"x": True}, changes({"x": True}, {"x": 1}))), b'{"x":1}'
        )
        for invalid in [
            [None],
            [{"op": "execute", "path": ["a"], "value": "anything"}],
            [{"op": "remove", "path": ["missing"]}],
            [{"op": "set", "path": ["list", "0"], "value": 3}],
            [
                {"op": "set", "path": ["a"], "value": {}},
                {"op": "set", "path": ["a", "x"], "value": 1},
            ],
        ]:
            with self.assertRaises(LocalError):
                apply(before, invalid)
        with self.assertRaisesRegex(LocalError, "delta_limit"):
            changes({}, {str(i): i for i in range(257)})


class DeltaCases:
    scope = working.WorkingTests.scope
    command = working.WorkingTests.command
    save = working.WorkingTests.save
    fails = retrieval.RetrievalTests.fails
    clock = retrieval.RetrievalTests.clock

    def adapter(self, base=None, **options):
        args = {
            **self.scope(),
            "checkpoint": None,
            "bindings": [{}],
            "max_output_tokens": 6000,
            "tokenizer": "cl100k_base",
            "adapter_context": {"model_session": "session", "context_epoch": 0},
            "base": base,
            **options,
        }
        return self.execute(self.command("adapter_resume", args))

    def ack(self, envelope):
        return {
            "id": envelope["base_id"],
            "hash": envelope["target_hash"],
            "sequence": envelope["sequence"],
            "schema": SCHEMA,
        }

    def test_full_delta_reconstruction_and_both_artifacts_replay_exactly(self):
        self.save()
        first = self.adapter()
        self.assertEqual(first["mode"], "full")
        base = materialize(first, None, first["binding"])
        checkpoint = working.checkpoint()
        checkpoint["current_state"] = "The revised patch still needs verification"
        self.save(checkpoint, revision=1)
        second = self.adapter(self.ack(first))
        self.assertEqual(second["mode"], "delta")
        self.assertIn(
            ["packet", "checkpoint", "state", "current_state"], second["essential_state_changes"]
        )
        self.assertTrue(
            any(
                item["kind"] == "checkpoint" and item["change"] == "added"
                for item in second["changed_references"]
            )
        )
        target = materialize(second, base, first["binding"])
        self.assertEqual(target["packet"]["checkpoint"]["state"], checkpoint)
        self.assertLess(len(canonical_bytes(second)), len(canonical_bytes(first)))
        wire = self.run_r("replay", {"project": "p", "receipt": second["receipt"]})
        full = self.run_r("replay", {"project": "p", "receipt": target["packet"]["receipt"]})
        self.assertEqual(wire, second)
        self.assertEqual(full, target["packet"])
        self.assertEqual(second["usage"]["materialized_bytes"], len(canonical_bytes(full)))
        broken = copy.deepcopy(base)
        broken["packet"]["checkpoint"]["state"]["objective"] = "tampered"
        with self.assertRaisesRegex(LocalError, "delta_base_mismatch"):
            materialize(second, broken, first["binding"])

    def test_missing_hash_schema_sequence_and_expired_bases_fall_back_current(self):
        self.save()
        first = self.adapter()
        for modification in (
            {"id": "absent"},
            {"hash": "0" * 64},
            {"schema": "different"},
            {"sequence": 100},
        ):
            result = self.adapter({**self.ack(first), **modification})
            self.assertEqual(result["mode"], "full")
            self.assertEqual(
                result["target"]["packet"]["checkpoint"]["state"], working.checkpoint()
            )
        clock = self.clock()
        clock.utc += 300001
        clock.mono += 300001
        self.assertEqual(self.adapter(self.ack(first))["fallback_reason"], "base_unavailable")

    def test_model_context_policy_change_and_revision_gap_force_new_full_state(self):
        self.save()
        first = self.adapter()
        reset = self.adapter(
            self.ack(first), adapter_context={"model_session": "new_session", "context_epoch": 1}
        )
        self.assertEqual(reset["mode"], "full")
        self.assertEqual(reset["fallback_reason"], "binding_changed")
        self.store.grant(self.owner, "another", "p", ["read"])
        changed = self.adapter(self.ack(first))
        self.assertEqual(changed["mode"], "full")
        self.assertEqual(changed["fallback_reason"], "binding_changed")
        self.save(revision=1)
        self.save(revision=2)
        gap = self.adapter(self.ack(changed))
        self.assertEqual(gap["mode"], "full")
        self.assertEqual(gap["fallback_reason"], "revision_gap")

    def test_purged_base_cannot_reconstruct_restricted_old_content(self):
        checkpoint = working.checkpoint()
        checkpoint["references"] = [{"kind": "observation", "id": self.obs["observation"]}]
        self.save(checkpoint)
        first = self.adapter()
        self.store.purge(self.owner, self.obs["observation"])
        self.fails("checkpoint_unavailable", lambda: self.adapter(self.ack(first)))
        self.save(revision=1)
        current = self.adapter(self.ack(first))
        self.assertEqual(current["mode"], "full")
        self.assertNotIn(self.obs["observation"], json.dumps(current))
        self.fails(
            "replay_purged",
            lambda: self.run_r("replay", {"project": "p", "receipt": first["receipt"]}),
        )

    def test_full_envelope_and_materialized_packet_budgets_are_separate(self):
        self.save()
        first = self.adapter()
        self.assertEqual(
            first["usage"]["materialized_tokens"],
            first["target"]["packet"]["usage"]["output_tokens"],
        )
        result = self.adapter(max_output_tokens=1)
        self.assertEqual(result["status"], "budget_insufficient")
        self.assertIsNone(result["base_id"])
        self.assertNotIn("target", result)
        self.assertFalse(result["usage"]["budget_compliant"])


class LocalDeltaTests(DeltaCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown
    execute = working.WorkingTests.execute
    run_r = retrieval.RetrievalTests.run_r
