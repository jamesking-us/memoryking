import hashlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from migration_helpers import historical_inventory

from memoryking.adapters.knowledge_store import Knowledge
from memoryking.adapters.local_store import LocalStore
from memoryking.domain.local import LocalError, SystemClock
from memoryking.domain.selectors import Work, canonical, contains, evaluate, intersection


def sel(*atoms):
    return {"selector_version": "1", "any_of": [{"all_of": list(atoms) or [{"op": "any"}]}]}


def eq(field, value):
    return {"field": field, "op": "eq", "value": value}


def region(path, env, lower, upper):
    return sel(
        eq("repository_id", "repo"),
        {"field": "path", "op": "path_under", "value": path},
        {"field": "environment", "op": "in", "value": env},
        {
            "field": "dependency_version",
            "op": "version_in",
            "dependency": "lib",
            "scheme": "numeric_triplet_v1",
            "value": {"lower": lower, "upper": upper},
        },
    )


class SelectorTests(unittest.TestCase):
    def test_overlap_and_containment(self):
        a = region("payments", ["production", "staging"], [1, 0, 0], [3, 0, 0])
        b = region("payments/worker", ["production"], [2, 0, 0], [4, 0, 0])
        overlap = intersection(a, b)
        self.assertEqual(
            overlap, canonical(region("payments/worker", ["production"], [2, 0, 0], [3, 0, 0]))
        )
        self.assertEqual(contains(overlap, a), "proven")
        self.assertEqual(contains(overlap, b), "proven")
        self.assertEqual(contains(b, a), "disproven")
        self.assertIsNone(
            intersection(sel(eq("environment", "prod")), sel(eq("environment", "stage")))
        )

    def test_three_values_and_binding_isolation(self):
        s = region("payments", ["production"], [1, 0, 0], [3, 0, 0])
        binding = {
            "repository_id": "repo",
            "path": "payments/x",
            "environment": "production",
            "dependencies": {"lib": [2, 0, 0]},
        }
        self.assertEqual(evaluate(s, binding), "true")
        self.assertEqual(evaluate(s, {**binding, "path": "payments-old/x"}), "false")
        self.assertEqual(evaluate(s, {**binding, "path": "payments"}), "false")
        self.assertEqual(evaluate(s, {**binding, "environment": None}), "unknown")
        self.assertEqual(evaluate(s, {**binding, "dependencies": {}}), "unknown")
        self.assertEqual(evaluate(s, {**binding, "dependencies": {"lib": None}}), "false")
        self.assertEqual(evaluate(s, {**binding, "dependencies": {"lib": "2.0.0-beta"}}), "unknown")
        self.assertEqual(
            evaluate(s, {**binding, "environment": "staging", "dependencies": {}}), "false"
        )

    def test_unsupported_limits_and_ancestry(self):
        for value in ["payments/*/retry?.py", "../secret", "/abs", "a//b", "a/./b", "a\\b"]:
            with self.assertRaises(LocalError):
                canonical(
                    sel(
                        eq("repository_id", "repo"),
                        {"field": "path", "op": "path_under", "value": value},
                    )
                )
        with self.assertRaises(LocalError):
            canonical(sel({"op": "exec", "value": "evil()"}))
        with self.assertRaises(LocalError):
            canonical(sel({"field": "path", "op": "path_under", "value": "src"}))
        with self.assertRaises(LocalError):
            intersection(sel(eq("environment", "prod")), sel(), Work(0))
        a = "a" * 40
        b = "b" * 40
        s = sel(eq("repository_id", "repo"), {"field": "commit", "op": "descendant_of", "value": a})
        self.assertEqual(evaluate(s, {"repository_id": "repo", "commit": a}), "true")
        self.assertEqual(evaluate(s, {"repository_id": "repo", "commit": b}), "unknown")
        other = sel(
            eq("repository_id", "repo"), {"field": "commit", "op": "descendant_of", "value": b}
        )
        self.assertIsNotNone(intersection(s, other))
        self.assertEqual(contains(s, other), "indeterminate")
        self.assertEqual(
            canonical(sel(eq("environment", "prod"), eq("environment", "prod"))),
            canonical(sel(eq("environment", "prod"))),
        )


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tenant = "main"
        self.temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-k-")
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.control = self.root / "control"
        self.owner = LocalStore.initialize(self.data, self.control).read_text()
        self.store = LocalStore(self.data, self.control)
        self.k = Knowledge(self.store)
        self.n = 0
        self.ns = self.store.namespace(self.owner, "p")
        self.raw = "Observed timeout=30. A result, not a universal rule."
        self.obs = self.store.capture(
            self.owner,
            self.ns,
            "e",
            {
                "version": 1,
                "project": "p",
                "source": "test",
                "source_event": "e",
                "text": self.raw,
                "readers": None,
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )
        self.span = {
            "observation": self.obs["observation"],
            "start_byte": 0,
            "end_byte": len(self.raw.encode()),
            "content_hash": hashlib.sha256(self.raw.encode()).hexdigest(),
        }

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def run_k(self, action, args, credential=None, key=None):
        self.n += 1
        command = {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": action,
            "args": args,
        }
        if action in {"propose", "review", "attest", "promote"}:
            command["key"] = key or f"k{self.n}"
        out = []
        self.k.execute(credential or self.owner, command).emit(out.append)
        return json.loads(out[0])

    def body(self, text="timeout=30", selector=None, **kw):
        return {
            "text": text,
            "kind": "knowledge",
            "selector": selector or sel(),
            "valid_start": None,
            "valid_end": None,
            "task": None,
            "run": None,
            "readers": None,
            "evidence": [self.span],
            "authority": None,
            "rule_key": None,
            "effect": None,
            "rule_value": None,
            "provenance": {},
            **kw,
        }

    def propose(self, body=None, key="timeout", version=0, **kw):
        return self.run_k(
            "propose",
            {
                "project": "p",
                "logical_key": key,
                "expected_version": version,
                "body": body or self.body(),
                **kw,
            },
        )

    def review(self, r, decision="accept", replacement=None, version=None, credential=None, **kw):
        args = {
            "project": "p",
            "revision": r["revision"],
            "expected_version": r["family_version"] if version is None else version,
            "decision": decision,
            "reason": "reviewed_evidence",
            "breadth_reviewed": True,
            **kw,
        }
        if replacement:
            args["replacement"] = replacement
        return self.run_k("review", args, credential)

    def query(self, **kw):
        return self.run_k("query", {"project": "p", "bindings": [{}], **kw})

    def constraints(self, credential=None, **kw):
        return self.run_k("constraints", {"project": "p", "bindings": [{}], **kw}, credential)

    def test_march_history_quarantine_no_resurrection(self):
        a = self.propose()
        a = self.review(a)
        before = self.store.status(self.owner, "p")["position"]
        b = self.propose(self.body("timeout=60", valid_start=300, valid_end=400), version=2)
        b = self.review(
            b,
            replacement={
                "predecessor": a["revision"],
                "selector": sel(),
                "valid_start": 300,
                "valid_end": 400,
            },
        )
        after = self.store.status(self.owner, "p")["position"]
        self.assertEqual(
            self.query(valid_ms=350, recorded_position=before)["items"][0]["revision"],
            a["revision"],
        )
        self.assertEqual(self.query(valid_ms=350)["items"][0]["revision"], b["revision"])
        self.assertEqual(self.query(valid_ms=400)["items"][0]["revision"], a["revision"])
        q = self.review(b, decision="quarantine")
        self.assertEqual(self.query(valid_ms=350)["status"], "blocked")
        self.assertEqual(self.query(valid_ms=350)["items"], [])
        old = self.query(valid_ms=350, recorded_position=after, diagnostic=True)
        self.assertTrue(old["items"][0]["quarantined"])
        self.review(q, decision="release_quarantine")
        self.assertEqual(self.query(valid_ms=350)["items"][0]["revision"], b["revision"])
        late = self.store.capture(
            self.owner,
            self.ns,
            "backdated",
            {
                "version": 1,
                "project": "p",
                "source": "test",
                "source_event": "backdated",
                "text": "backdated evidence",
                "readers": None,
                "classification": "internal",
                "observed_at_ms": 100,
            },
        )
        self.assertGreater(late["position"], after)
        self.assertEqual(
            self.query(valid_ms=350, recorded_position=before)["items"][0]["revision"],
            a["revision"],
        )
        self.store.purge(self.owner, self.obs["observation"])
        self.assertEqual([], self.query(valid_ms=350)["items"])
        self.assertEqual([], self.query(valid_ms=350, recorded_position=before)["items"])

    def test_partial_selector_replacement(self):
        a = self.propose(
            self.body(selector=region("payments", ["production", "staging"], [1, 0, 0], [3, 0, 0]))
        )
        a = self.review(a)
        b = self.propose(
            self.body(
                "new", selector=region("payments/worker", ["production"], [2, 0, 0], [4, 0, 0])
            ),
            version=2,
        )
        overlap = region("payments/worker", ["production"], [2, 0, 0], [3, 0, 0])
        with self.assertRaisesRegex(LocalError, "replacement_out_of_range"):
            self.review(
                b,
                replacement={
                    "predecessor": a["revision"],
                    "selector": region("payments/worker", ["production"], [2, 0, 0], [4, 0, 0]),
                    "valid_start": None,
                    "valid_end": None,
                },
            )
        self.review(
            b,
            replacement={
                "predecessor": a["revision"],
                "selector": overlap,
                "valid_start": None,
                "valid_end": None,
            },
        )
        binding = {
            "repository_id": "repo",
            "path": "payments/worker/x",
            "environment": "production",
            "dependencies": {"lib": [2, 5, 0]},
        }
        self.assertEqual(self.query(bindings=[binding])["items"][0]["revision"], b["revision"])
        self.assertEqual(
            self.query(bindings=[{**binding, "environment": "staging"}])["items"][0]["revision"],
            a["revision"],
        )
        self.assertEqual(
            self.query(bindings=[{**binding, "environment": None}])["status"], "blocked"
        )

    def test_concurrent_review_optimistic_version(self):
        r = self.propose()

        def decide(decision):
            try:
                return self.review(r, decision=decision)["state"]
            except LocalError as e:
                return e.code

        with ThreadPoolExecutor(2) as pool:
            result = list(pool.map(decide, ["accept", "reject"]))
        self.assertEqual(result.count("family_version_conflict"), 1)

    def test_breadth_legacy_authority_and_span_validation(self):
        r = self.propose()
        with self.assertRaisesRegex(LocalError, "breadth_review_required"):
            self.review(r, breadth_reviewed=False)
        reader = self.store.grant(self.owner, "reader", "p", ["read", "capture"])
        with self.assertRaisesRegex(LocalError, "denied"):
            self.review(r, credential=reader)
        legacy = self.propose(
            self.body(selector={"legacy": "*.py"}), key="legacy", preserve_legacy=True
        )
        with self.assertRaisesRegex(LocalError, "selector_unsupported"):
            self.review(legacy)
        self.assertTrue(
            any(
                x.get("selector_status") == "unsupported"
                for x in self.query(diagnostic=True)["items"]
            )
        )
        with self.assertRaisesRegex(LocalError, "denied"):
            self.run_k("query", {"project": "p", "bindings": [{}], "diagnostic": True}, reader)
        bad = self.body(evidence=[{**self.span, "end_byte": 9999}])
        with self.assertRaisesRegex(LocalError, "invalid_evidence_span"):
            self.propose(bad, key="bad")

    def test_no_curator_episode_and_proposal(self):
        self.propose(self.body("all retries fail"))
        out = []
        self.store.read(self.owner, "p", query="timeout").emit(out.append)
        episode = json.loads(out[0])["items"][0]
        self.assertEqual(episode["authority"], "observed_episode")
        self.assertEqual(episode["assurance"], "actor_reported")
        self.assertEqual(self.query()["items"], [])

    def test_constraints_complete_private_unknown_conflict_and_expiry(self):
        c = self.propose(
            self.body(
                "Must audit",
                kind="constraint",
                authority="project",
                rule_key="audit",
                effect="require",
                rule_value="yes",
                readers=["owner"],
            ),
            key="policy",
        )
        c = self.review(c)
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        denied = self.constraints(reader)
        self.assertEqual(denied["reason"], "essential_policy_unavailable")
        self.assertEqual(denied["items"], [])
        self.assertNotIn(c["revision"], json.dumps(denied))
        self.assertEqual(self.constraints()["status"], "complete")
        task = self.propose(
            self.body(
                "Skip audit",
                kind="constraint",
                authority="task",
                task="t",
                rule_key="audit",
                effect="prohibit",
                rule_value="yes",
            ),
            key="task",
        )
        self.review(task)
        self.assertEqual(self.constraints(task="t")["reason"], "unresolved_conflict")
        self.assertEqual(self.constraints()["reason"], "applicability_unknown")
        self.assertEqual(self.constraints(task="other")["status"], "complete")

    def test_policy_selector_unknown_and_explicit_absence(self):
        c = self.propose(
            self.body(
                kind="constraint",
                authority="project",
                rule_key="r",
                effect="require",
                rule_value="x",
                selector=region("src", ["prod"], [1, 0, 0], None),
            ),
            key="p",
        )
        self.review(c)
        self.assertEqual(self.constraints()["reason"], "applicability_unknown")
        self.assertEqual(
            self.constraints(
                bindings=[
                    {
                        "repository_id": "repo",
                        "path": "src/a",
                        "environment": "prod",
                        "dependencies": {"lib": None},
                    }
                ]
            )["status"],
            "complete",
        )

    def test_evidence_revocation_purge_and_restore(self):
        self.review(self.propose())
        backup = self.root / "backup"
        self.store.backup(backup)
        self.store.purge(self.owner, self.obs["observation"])
        self.assertIsNone(self.store.db.execute("SELECT body FROM revisions").fetchone()[0])
        self.store.close()
        LocalStore.restore(self.data, self.control, backup)
        self.owner = (self.control / "owner.credential").read_text()
        self.store = LocalStore(self.data, self.control)
        self.k = Knowledge(self.store)
        self.assertIsNone(self.store.db.execute("SELECT body FROM revisions").fetchone()[0])
        self.assertEqual(self.query()["items"], [])

    def test_backup_before_policy_fails_closed(self):
        backup = self.root / "backup"
        self.store.backup(backup)
        r = self.propose(
            self.body(
                kind="constraint",
                authority="project",
                rule_key="p",
                effect="prohibit",
                rule_value="x",
            )
        )
        self.review(r)
        old = self.store.incarnation
        self.store.close()
        LocalStore.restore(self.data, self.control, backup)
        self.owner = (self.control / "owner.credential").read_text()
        self.store = LocalStore(self.data, self.control)
        self.k = Knowledge(self.store)
        self.assertEqual(self.constraints()["reason"], "knowledge_reconciliation_required")
        with self.assertRaisesRegex(LocalError, "history_changed"):
            self.k.execute(
                self.owner,
                {
                    "version": 1,
                    "incarnation": old,
                    "action": "constraints",
                    "args": {"project": "p", "bindings": [{}]},
                },
            )

    def test_attestations_and_promotion_keep_authority_and_dependencies(self):
        r = self.review(self.propose())
        self.run_k(
            "attest",
            {
                "project": "p",
                "revision": r["revision"],
                "expires_ms": SystemClock().utc_ms() + 100000,
                "statement": "tested explicit evidence",
            },
        )
        self.assertEqual(self.query()["items"][0]["attestations"][0]["actor"], "owner")
        promoted = self.run_k(
            "promote",
            {
                "project": "other",
                "revision": r["revision"],
                "logical_key": "copy",
                "expected_version": 0,
            },
        )
        self.assertEqual(promoted["state"], "proposed")
        self.store.purge(self.owner, self.obs["observation"])
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM revisions WHERE purged=1").fetchone()[0], 2
        )

    def test_mutation_retry_identical_and_mismatched(self):
        args = {"project": "p", "logical_key": "retry", "expected_version": 0, "body": self.body()}
        a = self.run_k("propose", args, key="stable")
        b = self.run_k("propose", args, key="stable")
        self.assertEqual(a["revision"], b["revision"])
        with self.assertRaisesRegex(LocalError, "idempotency_conflict"):
            self.run_k("propose", {**args, "logical_key": "different"}, key="stable")

    def test_current_policy_expiry_uses_one_persisted_instant(self):
        class MovingClock(SystemClock):
            offset = 0

            def utc_ms(self):
                return super().utc_ms() + self.offset

            def monotonic_ms(self):
                return super().monotonic_ms() + self.offset

        clock = MovingClock()
        self.store.clock = clock
        boundary = clock.utc_ms() + 1500
        a = self.propose(
            self.body(
                kind="constraint",
                authority="project",
                rule_key="a",
                effect="require",
                rule_value="x",
                valid_end=boundary,
            ),
            key="expire",
        )
        self.review(a)
        b = self.propose(
            self.body(
                kind="constraint",
                authority="project",
                rule_key="b",
                effect="require",
                rule_value="x",
                valid_start=boundary,
            ),
            key="activate",
        )
        self.review(b)

        def stall(point):
            if point == "after_validation":
                clock.offset += 2000

        self.store.fault = stall
        first = self.constraints()
        self.store.fault = lambda _: None
        self.assertEqual([x["revision"] for x in first["items"]], [a["revision"]])
        self.assertLess(first["evaluated_ms"], boundary)
        self.assertEqual([x["revision"] for x in self.constraints()["items"]], [b["revision"]])

    def test_purged_policy_blocks_until_explicit_authorized_deprecation(self):
        r = self.propose(
            self.body(
                kind="constraint",
                authority="project",
                rule_key="p",
                effect="prohibit",
                rule_value="x",
            )
        )
        r = self.review(r)
        self.store.purge(self.owner, self.obs["observation"])
        self.assertEqual(self.constraints()["reason"], "essential_policy_unavailable")
        self.review(r, decision="deprecate")
        self.assertEqual(self.constraints()["status"], "complete")

    def test_registry_limit_and_cross_binding_conflicts(self):
        from unittest.mock import patch

        a = self.propose(
            self.body(
                kind="constraint",
                authority="project",
                rule_key="r",
                effect="require",
                rule_value="x",
                selector=sel(eq("environment", "prod")),
            ),
            key="prod",
        )
        self.review(a)
        b = self.propose(
            self.body(
                kind="constraint",
                authority="project",
                rule_key="r",
                effect="prohibit",
                rule_value="x",
                selector=sel(eq("environment", "test")),
            ),
            key="test",
        )
        self.review(b)
        result = self.constraints(bindings=[{"environment": "prod"}, {"environment": "test"}])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["items"]), 2)
        with patch("memoryking.adapters.knowledge_store.REGISTRY_LIMIT", 1):
            self.assertEqual(self.constraints()["reason"], "constraint_resolution_incomplete")

    def test_migration_preserves_version_one_capture(self):
        self.store.close()
        import sqlite3

        db = sqlite3.connect(self.data / "store.sqlite")
        for table in [
            "attestations",
            "conflicts",
            "replacements",
            "lifecycle",
            "evidence_spans",
            "revisions",
            "families",
            "knowledge_state",
        ]:
            db.execute("DROP TABLE " + table)
        historical_inventory(db, 1)
        db.commit()
        db.close()
        self.store = LocalStore(self.data, self.control)
        self.k = Knowledge(self.store)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 9)
        self.assertEqual(
            self.store.db.execute("SELECT id FROM observations").fetchone()[0],
            self.obs["observation"],
        )
        self.assertEqual(self.query()["status"], "complete")

    def test_transitive_visibility_on_promoted_evidence(self):
        source = self.review(self.propose())
        promoted = self.run_k(
            "promote",
            {
                "project": "q",
                "revision": source["revision"],
                "logical_key": "derived",
                "expected_version": 0,
            },
        )
        self.run_k(
            "review",
            {
                "project": "q",
                "revision": promoted["revision"],
                "expected_version": 1,
                "decision": "accept",
                "reason": "reviewed",
                "breadth_reviewed": True,
            },
        )
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        self.store.grant(self.owner, "reader", "q", ["read"])
        query = {"project": "q", "bindings": [{}]}
        self.assertEqual(len(self.run_k("query", query, reader)["items"]), 1)
        self.store.revoke(self.owner, "reader", "p")
        self.assertEqual(self.run_k("query", query, reader)["items"], [])

    def test_tenant_workspace_policy_cannot_be_silently_omitted(self):
        for scope in ["_tenant", "_workspace"]:
            proposal = self.run_k(
                "propose",
                {
                    "project": scope,
                    "logical_key": "global",
                    "expected_version": 0,
                    "body": self.body(
                        kind="constraint",
                        authority="project",
                        rule_key=scope,
                        effect="require",
                        rule_value="x",
                    ),
                },
            )
            self.run_k(
                "review",
                {
                    "project": scope,
                    "revision": proposal["revision"],
                    "expected_version": 1,
                    "decision": "accept",
                    "reason": "global_policy",
                    "breadth_reviewed": True,
                },
            )
        self.assertEqual(len(self.constraints()["items"]), 2)
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        self.assertEqual(self.constraints(reader)["reason"], "essential_policy_unavailable")
        self.store.grant(self.owner, "reader", "_tenant", ["read"])
        self.store.grant(self.owner, "reader", "_workspace", ["read"])
        self.assertEqual(len(self.constraints(reader)["items"]), 2)

    def test_promotion_requires_destination_capture_and_policy_retry_reauthorizes(self):
        accepted = self.review(self.propose())
        actor = self.store.grant(self.owner, "reviewer", "p", ["read", "review", "export"])
        self.store.grant(self.owner, "reviewer", "other", ["read", "review"])
        args = {
            "project": "other",
            "revision": accepted["revision"],
            "logical_key": "copy",
            "expected_version": 0,
        }
        with self.assertRaisesRegex(LocalError, "denied"):
            self.run_k("promote", args, actor, "promote")
        self.store.grant(self.owner, "reviewer", "other", ["read", "review", "capture"])
        self.assertEqual(self.run_k("promote", args, actor, "promote")["state"], "proposed")
        policy = self.store.grant(self.owner, "author", "p", ["read", "capture", "policy"])
        args = {
            "project": "p",
            "logical_key": "rule",
            "expected_version": 0,
            "body": self.body(
                kind="constraint",
                authority="project",
                rule_key="network",
                effect="prohibit",
                rule_value="external",
            ),
        }
        self.run_k("propose", args, policy, "policy")
        self.store.grant(self.owner, "author", "p", ["read", "capture"])
        with self.assertRaisesRegex(LocalError, "denied"):
            self.run_k("propose", args, policy, "policy")
        with self.assertRaisesRegex(LocalError, "invalid_constraint"):
            self.propose(
                self.body(kind="constraint", rule_key="x", effect="require", rule_value="y"),
                key="invalid",
            )

    def test_replacement_masks_count_toward_aggregate_work_limit(self):
        original = self.review(self.propose())
        successor = self.propose(self.body("replacement"), version=2)
        self.review(
            successor,
            replacement={
                "predecessor": original["revision"],
                "selector": sel(),
                "valid_start": None,
                "valid_end": None,
            },
        )
        # Artificially large durable mask inventory tests fail-closed enumeration.
        db = self.store.db
        edge = db.execute("SELECT * FROM replacements").fetchone()
        columns = [d[1] for d in db.execute("PRAGMA table_info(replacements)")]
        self.assertIn("position", columns)
        for _n in range(300):
            values = list(edge)
            values[columns.index("position")] = self.store._event(
                edge["tenant"], "review", original["revision"], SystemClock().utc_ms(), "owner"
            )
            db.execute("INSERT INTO replacements VALUES (?,?,?,?,?,?,?)", values)
        result = self.query(bindings=[{} for _ in range(256)])
        self.assertEqual(result["reason"], "selector_limit_exceeded")
        self.assertEqual(result["items"], [])
