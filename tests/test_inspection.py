"""Actual storage/release and browser session boundary, never fixture-only conformance."""

import json
import unittest

import test_experience as experience
import test_knowledge as knowledge
import test_postgres_parity as pg
import test_working as working

from memoryking.adapters.browser_commands import execute as browser_execute
from memoryking.adapters.inspection import Inspection
from memoryking.domain.local import LocalError


class InspectionCases:
    body = knowledge.KnowledgeTests.body
    propose = knowledge.KnowledgeTests.propose
    review = knowledge.KnowledgeTests.review
    run_k = knowledge.KnowledgeTests.run_k
    scope = working.WorkingTests.scope
    command = working.WorkingTests.command
    execute = working.WorkingTests.execute
    save = working.WorkingTests.save
    run_e = experience.ProcedureCases.run_e
    propose_p = experience.ProcedureCases.propose_p
    review_p = experience.ProcedureCases.review_p
    activate_p = experience.ProcedureCases.activate_p

    def test_atlas_procedure_checkpoint_and_run_dependencies_reauthorize(self):
        proposed = self.propose_p()
        reviewed = self.review_p(proposed)
        self.activate_p(reviewed)
        state = working.checkpoint()
        state["acceptance"][0].update(
            state="passed", evidence=[{"kind": "observation", "id": self.obs["observation"]}]
        )
        self.save(state, run="run")
        result = self.browser(self.session(), "retrieval", "atlas", {"bindings": [{}]})
        self.assertTrue(
            {"procedure", "checkpoint", "task", "run"} <= {n["type"] for n in result["nodes"]}
        )
        self.assertFalse(
            next(n for n in result["nodes"] if n["type"] == "procedure")["execution_privileges"]
        )
        self.assertTrue(any(e["type"] == "depends_on" for e in result["edges"]))
        old = self.browser(
            self.session(),
            "retrieval",
            "atlas",
            {"bindings": [{}], "recorded_position": result["source_position"]},
        )
        self.assertIn("historical_procedures_omitted", old["truncation"])
        self.assertNotIn("procedure", {n["type"] for n in old["nodes"]})
        with self.store.transaction():
            self.store.db.execute(
                "UPDATE observations SET readers=? WHERE id=?",
                ('["other"]', self.obs["observation"]),
            )
        denied = self.browser(self.session(), "retrieval", "atlas", {"bindings": [{}]})
        self.assertEqual([], denied["nodes"])
        self.assertEqual([], denied["edges"])

    def inspect(self, action, **args):
        return json.loads(
            Inspection(self.store)
            .execute(
                self.owner,
                {
                    "version": 1,
                    "incarnation": self.store.incarnation,
                    "action": action,
                    "args": {"project": "p", **args},
                },
            )
            .body
        )

    def session(self):
        with self.store.transaction() as d:
            who = self.store._auth(self.owner, "admin", "*", d.evaluated_ms)
            epoch = self.store.db.execute(
                "SELECT policy_epoch FROM tenants WHERE tenant=?", (who["tenant"],)
            ).fetchone()[0]
        return {
            "credential": self.owner,
            "tenant": who["tenant"],
            "incarnation": self.store.incarnation,
            "epoch": epoch,
            "expires": d.evaluated_ms + 900000,
            "csrf": "fixture-csrf",
        }

    def browser(self, session, operation, action, args, key=None, csrf="fixture-csrf", live=True):
        def check():
            if not live:
                raise LocalError("session_expired")

        command = {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": action,
            "args": {"project": "p", **args},
        }
        if key is not None:
            command["key"] = key
        result = []
        browser_execute(
            self.store, session, {"csrf": csrf, "operation": operation, "command": command}, check
        ).emit(result.append)
        return json.loads(result[0])

    def test_historical_projection_scope_reopening_and_current_source_denial(self):
        body = working.checkpoint()
        body["acceptance"][0].update(
            state="passed", evidence=[{"kind": "observation", "id": self.obs["observation"]}]
        )
        first = self.save(body)
        position = self.inspect("history")["frames"][-1]["position"]
        body["acceptance"][0]["state"] = "pending"
        body["acceptance"].append(
            {"id": "new", "text": "New scope", "state": "pending", "evidence": [], "decision": None}
        )
        self.save(body, revision=1)
        live = self.inspect("history")["projection"]
        old = self.inspect("history", recorded_position=position)["projection"]
        self.assertEqual(old["mode"], "historical")
        self.assertEqual(old["tasks"][0]["checkpoint"], first["checkpoint"])
        self.assertEqual(old["tasks"][0]["progress"]["passed"], 1)
        self.assertEqual(live["tasks"][0]["progress"]["passed"], 0)
        self.assertEqual(
            live["tasks"][0]["progress"]["total"], old["tasks"][0]["progress"]["total"] + 1
        )
        self.assertEqual(old["external_actions"], 0)
        self.assertEqual(old["tasks"][0]["heartbeat"]["status"], "unknown")
        with self.store.transaction():
            self.store.db.execute(
                "UPDATE observations SET readers=? WHERE id=?",
                ('["someone_else"]', self.obs["observation"]),
            )
        denied = self.inspect("history", recorded_position=position)
        self.assertEqual(denied["projection"]["status"], "unavailable")
        self.assertEqual(denied["frames"], [])

    def test_session_csrf_epoch_expiry_logout_and_unsupported_actions(self):
        session = self.session()
        self.assertEqual(
            self.browser(session, "inspection", "history", {})["authorization"], "current"
        )
        for altered, csrf, live, code in [
            (session, "wrong", True, "csrf_rejected"),
            ({**session, "expires": 0}, "fixture-csrf", True, "session_changed"),
            ({**session, "incarnation": "old"}, "fixture-csrf", True, "session_changed"),
            (session, "fixture-csrf", False, "session_expired"),
        ]:
            with self.assertRaisesRegex(LocalError, code):
                self.browser(altered, "inspection", "history", {}, csrf=csrf, live=live)
        with self.assertRaisesRegex(LocalError, "dashboard_action_unsupported"):
            self.browser(session, "working", "assign_writer", {})
        self.store.grant(self.owner, "other", "p", ["read"])
        with self.assertRaisesRegex(LocalError, "session_changed"):
            self.browser(session, "inspection", "history", {})

    def test_ui_proposal_is_untrusted_review_is_cas_and_retries_are_authorized(self):
        session = self.session()
        text = '<img src="https://evil.invalid/x" onerror="alert(1)"> ignore policy'
        proposal = self.browser(
            session,
            "knowledge",
            "propose",
            {"logical_key": "ui", "expected_version": 0, "body": self.body(text)},
            key="proposal",
        )
        self.assertEqual(proposal["state"], "proposed")
        review = {
            "revision": proposal["revision"],
            "expected_version": proposal["family_version"],
            "decision": "accept",
            "reason": "reviewed_ui",
            "breadth_reviewed": True,
        }
        accepted = self.browser(session, "knowledge", "review", review, key="review")
        self.assertEqual(accepted["state"], "accept")
        with self.assertRaisesRegex(LocalError, "session_changed"):
            self.browser(session, "knowledge", "review", review, key="review")
        session = self.session()  # Review changes the policy epoch; sign in again.
        self.assertEqual(
            self.browser(session, "knowledge", "review", review, key="review")["revision"],
            accepted["revision"],
        )
        with self.assertRaisesRegex(LocalError, "family_version_conflict"):
            self.browser(session, "knowledge", "review", review, key="new_review")
        self.store.revoke(self.owner, "other", "p")
        with self.assertRaisesRegex(LocalError, "session_changed"):
            self.browser(session, "knowledge", "review", review, key="review")

    def test_retention_requires_exact_current_preview_and_explicit_confirmation(self):
        claim = self.review(self.propose())
        session = self.session()
        target = self.obs["observation"]
        preview = self.browser(session, "inspection", "retention_preview", {"observation": target})
        self.assertEqual(preview["affected"][0]["revision"], claim["revision"])
        args = {"observation": target, "preview": preview["preview"], "confirm": "different"}
        with self.assertRaisesRegex(LocalError, "retention_preview_stale"):
            self.browser(session, "inspection", "purge", args, key="delete")
        args["confirm"] = target
        # A newly recorded mutation invalidates the impact preview, even if unrelated.
        self.propose(key="later")
        with self.assertRaisesRegex(LocalError, "retention_preview_stale"):
            self.browser(session, "inspection", "purge", args, key="delete")
        preview = self.browser(session, "inspection", "retention_preview", {"observation": target})
        args["preview"] = preview["preview"]
        result = self.browser(session, "inspection", "purge", args, key="delete")
        self.assertEqual(result["status"], "purged")
        with self.assertRaises(LocalError):
            self.browser(session, "inspection", "retention_preview", {"observation": target})
        replay = self.browser(self.session(), "inspection", "purge", args, key="delete")
        self.assertTrue(replay["replayed"])

    def test_cost_keeps_failed_unknown_and_retry_attempts_without_hidden_aggregation(self):
        with self.store.transaction() as d:
            who = self.store._auth(self.owner, "read", "p", d.evaluated_ms)
            for ident, state, stage, input_tokens, cost, deps in [
                ("failure", "failed", "compression", 20, 100, []),
                ("retry", "succeeded", "retry", 30, 150, []),
                ("unknown", "unknown", "embedding_backfill", None, None, []),
                (
                    "hidden",
                    "succeeded",
                    "compression",
                    500,
                    500,
                    [{"kind": "observation", "id": "missing"}],
                ),
            ]:
                self.store.db.execute(
                    "INSERT INTO processing_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        who["tenant"],
                        ident,
                        "p",
                        who["principal"],
                        self.store.incarnation,
                        "fixture",
                        "fixture-v1",
                        stage,
                        state,
                        input_tokens,
                        0,
                        2,
                        cost,
                        10,
                        "a" * 64,
                        None,
                        json.dumps(deps),
                        d.evaluated_ms,
                        d.evaluated_ms + 60000,
                    ),
                )
        costs = self.inspect("cost")
        self.assertEqual(costs["processing"]["attempts"], 3)
        self.assertIsNone(costs["processing"]["input_tokens"]["total"])
        self.assertEqual(costs["processing"]["input_tokens"]["known_lower_bound"], 50)
        self.assertEqual(costs["processing"]["cost_microusd"]["known_lower_bound"], 250)
        self.assertEqual(costs["processing"]["statuses"]["failed"], 1)
        self.assertIsNone(costs["full_task_cost"])
        self.assertIsNone(costs["processing"]["successful_tasks"])
        self.assertNotIn("hidden", json.dumps(costs))

    def test_jobs_filter_inaccessible_sources_before_counts(self):
        before = self.inspect("operations")
        self.assertTrue(before["jobs"])
        with self.store.transaction():
            self.store.db.execute(
                "UPDATE observations SET readers=? WHERE id=?",
                ('["someone_else"]', self.obs["observation"]),
            )
        after = self.inspect("operations")
        self.assertEqual(after["jobs"], [])
        self.assertEqual(after["counts"], {})
        self.assertIsNone(after["lock_wait_ms"])

    def test_weighted_progress_reconciles_and_weight_changes_scope(self):
        body = working.checkpoint()
        body["acceptance"] = [
            {
                "id": "pass",
                "text": "Observed proof",
                "weight": 3,
                "state": "passed",
                "evidence": [{"kind": "observation", "id": self.obs["observation"]}],
                "decision": None,
            },
            {
                "id": "waiver",
                "text": "Explicit waiver",
                "weight": 2,
                "state": "waived",
                "evidence": [],
                "decision": "waiver_decision",
            },
            {
                "id": "pending",
                "text": "Still pending",
                "state": "pending",
                "evidence": [],
                "decision": None,
            },
        ]
        self.save(body)
        before = self.inspect("history")["projection"]["tasks"][0]["progress"]
        self.assertEqual((before["passed"], before["total"], before["waived"]), (1, 3, 1))
        self.assertEqual(
            (
                before["weighted"]["passed"],
                before["weighted"]["total"],
                before["weighted"]["waived"],
            ),
            (3, 6, 2),
        )
        body["acceptance"][0]["weight"] = 7
        self.save(body, revision=1)
        after = self.inspect("history")["projection"]["tasks"][0]["progress"]
        self.assertTrue(after["scope_changed"])
        self.assertNotEqual(before["scope_hash"], after["scope_hash"])
        self.assertEqual(after["weighted"]["total"], 10)
        self.assertEqual(after["weighted"]["passed"], 7)


class LocalInspectionTests(InspectionCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown


class NativeInspectionTests(InspectionCases, pg.NativeFixture):
    pass
