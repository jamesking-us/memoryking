"""Real local repository/barrier/knowledge integration, without worker execution."""

import json
import subprocess
import unittest

import test_knowledge as helpers
from migration_helpers import historical_inventory
from test_local_store import FakeClock

from memoryking.adapters.local_ipc import StoreOperations
from memoryking.adapters.repository_store import Repository
from memoryking.adapters.retrieval import Retrieval
from memoryking.domain.local import LocalError


class RepositoryTests(unittest.TestCase):
    tearDown = helpers.KnowledgeTests.tearDown
    run_k = helpers.KnowledgeTests.run_k
    body = helpers.KnowledgeTests.body
    propose = helpers.KnowledgeTests.propose
    review = helpers.KnowledgeTests.review
    query = helpers.KnowledgeTests.query
    constraints = helpers.KnowledgeTests.constraints

    def setUp(self):
        helpers.KnowledgeTests.setUp(self)
        self.repo = self.root / "code"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        (self.repo / "a.py").write_text("def value():\n    return 1\n")
        (self.repo / "b.py").write_text("other = 2\n")
        self.git("add", ".")
        self.git("commit", "-qm", "initial")
        self.rid = self.run_repo(
            "register", remote="local:fixture", clone="first", path=str(self.repo), readers=None
        )["repository"]
        self.first = self.capture()
        self.claim = self.propose()
        self.review(self.claim)

    def test_git_runs_outside_the_store_transaction(self):
        from unittest.mock import patch

        from memoryking.adapters import repository_git

        locked = []
        original = repository_git.GitSnapshot.capture

        def observed(snapshot, repository, environment):
            locked.append(self.store.lock._is_owned())
            return original(snapshot, repository, environment)

        (self.repo / "a.py").write_text("def value():\n    return 3\n")
        with patch.object(repository_git.GitSnapshot, "capture", observed):
            result = self.capture()
        self.assertEqual(locked, [False])
        self.assertEqual(result["status"], "captured")
        self.assertTrue(result["dirty"])

    def git(self, *args):
        return (
            subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.DEVNULL)
            .decode()
            .strip()
        )

    def run_repo(self, action, credential=None, key=None, **args):
        self.n += 1
        command = {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": action,
            "args": {"project": "p", **args},
        }
        if action not in {"inspect", "impact", "coverage", "pending", "history"}:
            command["key"] = key or f"repo{self.n}"
        out = []
        StoreOperations(self.store).execute(
            {
                "version": 1,
                "credential": credential or self.owner,
                "operation": "repository",
                "args": {"command": command},
            }
        ).emit(out.append)
        return json.loads(out[0])

    def capture(self, **kw):
        return self.run_repo(
            "capture", repository=self.rid, clone="first", environment="test", **kw
        )

    def binding(self, snap=None, **kw):
        snap = snap or self.first
        return {
            "repository_id": self.rid,
            "snapshot_id": snap["snapshot"],
            "commit": snap["commit"],
            "environment": "test",
            "path": "a.py",
            **kw,
        }

    def bind(self, claim=None, snap=None, **kw):
        return self.run_repo(
            "bind",
            revision=(claim or self.claim)["revision"],
            repository=self.rid,
            snapshot=(snap or self.first)["snapshot"],
            dependencies=[{"kind": "file", "target": "a.py", "policy": "invalidate"}],
            impact="high",
            coverage_reviewed=True,
            expected_policy=0,
            evidence=self.obs["observation"],
            **kw,
        )

    def verify(self, claim=None, snap=None, **kw):
        return self.run_repo(
            "verify",
            revision=(claim or self.claim)["revision"],
            snapshot=(snap or self.first)["snapshot"],
            expected_policy=1,
            evidence=self.obs["observation"],
            passed=True,
            expires_ms=self.store.clock.utc_ms() + 600000,
            **kw,
        )

    def worker(self, snap):
        return self.run_repo(
            "revalidate",
            repository=self.rid,
            snapshot=snap["snapshot"],
            expected_generation=snap["generation"],
        )

    def test_paused_workers_unseen_and_changed_snapshot_block_guidance(self):
        self.bind()
        self.assertEqual(self.query(bindings=[self.binding()])["status"], "blocked")
        self.verify()
        self.assertEqual(
            self.query(bindings=[self.binding()])["items"][0]["revision"], self.claim["revision"]
        )
        independent = self.propose(self.body("independent knowledge"), key="independent")
        self.review(independent)
        (self.repo / "a.py").write_text("def value():\n    return 3\n")
        changed = self.capture()
        result = self.query(bindings=[self.binding(changed)])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual([i["revision"] for i in result["items"]], [independent["revision"]])
        result = self.query(bindings=[self.binding(snapshot_id="snap_unseen")])
        self.assertEqual([i["revision"] for i in result["items"]], [independent["revision"]])
        self.assertEqual(self.worker(changed)["results"][0]["state"], "invalid")
        self.assertEqual(
            [i["revision"] for i in self.query(bindings=[self.binding(changed)])["items"]],
            [independent["revision"]],
        )
        self.assertEqual(self.query(bindings=[self.binding()])["status"], "complete")

    def test_unchanged_hashes_need_trusted_comparison_and_environment(self):
        self.bind()
        self.verify()
        (self.repo / "b.py").write_text("other = 4\n")
        second = self.capture()
        self.assertEqual(self.query(bindings=[self.binding(second)])["status"], "complete")
        result = self.worker(second)
        self.assertEqual(result["results"][0]["state"], "valid")
        self.assertFalse(result["checks_executed"])
        self.assertEqual(self.query(bindings=[self.binding(second)])["status"], "complete")
        other = self.run_repo("capture", repository=self.rid, clone="first", environment="prod")
        self.assertEqual(self.worker(other)["results"][0]["reason"], "environment_changed")
        self.assertEqual(
            self.query(bindings=[self.binding(other, environment="prod")])["status"], "blocked"
        )

    def test_generation_fence_policy_cas_and_exact_capture_retry(self):
        self.bind()
        self.verify()
        (self.repo / "b.py").write_text("other = 4\n")
        second = self.capture(key="capture-retry")
        (self.repo / "a.py").write_text("changed = 9\n")
        retried = self.capture(key="capture-retry")
        self.assertEqual(second["snapshot"], retried["snapshot"])
        latest = self.capture()
        with self.assertRaisesRegex(LocalError, "repository_generation_conflict"):
            self.worker(second)
        with self.assertRaisesRegex(LocalError, "dependency_policy_conflict"):
            self.bind()
        with self.assertRaisesRegex(LocalError, "dependency_policy_stale"):
            self.verify(snap=latest)

    def test_missing_dependencies_and_unsupported_symbols_stay_unknown(self):
        self.bind()
        self.verify()
        (self.repo / "a.py").unlink()
        missing = self.capture()
        self.assertEqual(self.worker(missing)["results"][0]["reason"], "dependency_missing")
        self.assertEqual(self.query(bindings=[self.binding(missing)])["status"], "blocked")
        with self.assertRaisesRegex(LocalError, "dependency_coverage_incomplete"):
            self.run_repo(
                "bind",
                revision=self.claim["revision"],
                repository=self.rid,
                snapshot=missing["snapshot"],
                dependencies=[
                    {"kind": "symbol", "target": "unknown:foo:1", "policy": "revalidate"}
                ],
                impact="high",
                coverage_reviewed=True,
                expected_policy=1,
                evidence=self.obs["observation"],
            )

    def test_trusted_ancestry_future_valid_and_unrelated_branch(self):
        base = self.first["commit"]
        self.git("checkout", "-qb", "feature")
        (self.repo / "feature").write_text("new")
        self.git("add", ".")
        self.git("commit", "-qm", "feature")
        feature = self.capture()
        claim = self.propose(
            self.body(
                "feature-only",
                selector=helpers.sel(
                    helpers.eq("repository_id", self.rid),
                    {"field": "commit", "op": "descendant_of", "value": feature["commit"]},
                ),
            ),
            key="feature",
        )
        self.review(claim)
        self.assertIn(
            claim["revision"],
            [i["revision"] for i in self.query(bindings=[self.binding(feature)])["items"]],
        )
        self.git("checkout", "-qb", "other", base)
        (self.repo / "other").write_text("other")
        self.git("add", ".")
        self.git("commit", "-qm", "other")
        other = self.capture()
        self.assertNotIn(
            claim["revision"],
            [i["revision"] for i in self.query(bindings=[self.binding(other)])["items"]],
        )
        future = self.propose(
            self.body("future", valid_start=self.store.clock.utc_ms() + 100000), key="future"
        )
        self.review(future)
        self.assertNotIn(
            future["revision"],
            [i["revision"] for i in self.query(bindings=[self.binding(feature)])["items"]],
        )

    def test_scope_coverage_and_impact_are_read_only_and_bounded(self):
        self.bind()
        self.verify()
        args = {"repository": self.rid, "snapshot": self.first["snapshot"]}
        result = self.run_repo(
            "coverage", **args, paths=["a.py", "b.py"], bindings=[self.binding()]
        )
        self.assertEqual(result["missing_paths"], ["b.py"])
        result = self.run_repo(
            "coverage",
            **args,
            paths=["a.py", "b.py"],
            bindings=[self.binding(), self.binding(path="b.py")],
        )
        self.assertEqual(result["status"], "complete")
        before = self.store.db.execute("SELECT count(*) FROM code_validations").fetchone()[0]
        preview = self.run_repo("impact", **args, paths=["a.py"], limit=128)
        self.assertFalse(preview["mutation"])
        self.assertEqual(preview["edges"][0]["source"], self.claim["revision"])
        self.assertEqual(preview["edges"][0]["status"], "unchanged")
        self.assertEqual(
            before, self.store.db.execute("SELECT count(*) FROM code_validations").fetchone()[0]
        )
        self.assertTrue(self.run_repo("impact", **args, paths=["a.py"], limit=1)["truncated"])

    def test_revoked_evidence_and_expiry_block_materialization(self):
        self.bind()
        self.verify()
        clock = FakeClock()
        clock.utc = self.store.clock.utc_ms() + 700000
        self.store.clock = clock
        self.assertEqual(self.query(bindings=[self.binding()])["status"], "blocked")

    def test_capabilities_and_private_repo_visibility(self):
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        with self.assertRaisesRegex(LocalError, "denied"):
            self.run_repo("capture", reader, repository=self.rid, clone="first", environment="test")
        self.assertEqual(
            self.run_repo("inspect", reader, repository=self.rid, snapshot=self.first["snapshot"])[
                "snapshot"
            ],
            self.first["snapshot"],
        )
        self.bind()
        self.verify()
        # A narrowed evidence source must remove dependency edges and guidance.
        self.store.db.execute(
            "UPDATE observations SET readers=? WHERE id=?",
            (json.dumps(["owner"]), self.obs["observation"]),
        )
        self.store.db.commit()
        result = self.run_repo(
            "impact",
            reader,
            repository=self.rid,
            snapshot=self.first["snapshot"],
            paths=["a.py"],
            limit=128,
        )
        self.assertEqual(result["edges"], [])

    def test_mandatory_snapshot_policy_blocks_brief(self):
        c = self.propose(
            self.body(
                "do not change timeout",
                kind="constraint",
                authority="project",
                rule_key="timeout",
                effect="prohibit",
                rule_value="change",
            ),
            key="required",
        )
        self.review(c)
        self.bind(claim=c)
        args = {
            "project": "p",
            "query": "timeout",
            "bindings": [self.binding()],
            "max_output_tokens": 6000,
            "tokenizer": "cl100k_base",
        }
        out = []
        Retrieval(self.store).execute(
            self.owner,
            {"version": 1, "incarnation": self.store.incarnation, "action": "brief", "args": args},
        ).emit(out.append)
        result = json.loads(out[0])
        self.assertEqual(result["status"], "blocked")
        self.verify(claim=c)
        self.assertEqual(self.constraints(bindings=[self.binding()])["status"], "complete")

    def test_retained_briefing_requires_current_validation_proof(self):
        self.bind()
        self.verify()
        args = {
            "project": "p",
            "query": "timeout",
            "bindings": [self.binding()],
            "max_output_tokens": 6000,
            "tokenizer": "cl100k_base",
        }
        out = []
        Retrieval(self.store).execute(
            self.owner,
            {"version": 1, "incarnation": self.store.incarnation, "action": "brief", "args": args},
        ).emit(out.append)
        result = json.loads(out[0])
        deps = json.loads(
            self.store.db.execute(
                "SELECT dependencies FROM rendered_artifacts WHERE id=?", (result["receipt"],)
            ).fetchone()[0]
        )
        self.assertIn("code_validation", [d["kind"] for d in deps])
        replay = {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": "replay",
            "args": {"project": "p", "receipt": result["receipt"]},
        }
        again = []
        Retrieval(self.store).execute(self.owner, replay).emit(again.append)
        self.assertEqual(out, again)
        self.store.db.execute("UPDATE code_validations SET state='invalid'")
        self.store.db.commit()
        with self.assertRaisesRegex(LocalError, "replay_denied"):
            Retrieval(self.store).execute(self.owner, replay)

    def test_multi_snapshot_scope_never_emits_partially_validated_claim(self):
        self.bind()
        self.verify()
        result = self.query(bindings=[self.binding(), self.binding(snapshot_id="snap_unseen")])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["items"], [])

    def test_return_to_old_snapshot_advances_barrier_and_fences_worker(self):
        (self.repo / "b.py").write_text("other = 8\n")
        second = self.capture()
        (self.repo / "b.py").write_text("other = 2\n")
        original = self.capture()
        self.assertEqual(original["snapshot"], self.first["snapshot"])
        self.assertGreater(original["generation"], second["generation"])
        with self.assertRaisesRegex(LocalError, "repository_generation_conflict"):
            self.worker(second)

    def test_restore_retires_snapshot_validation_and_request_namespace(self):
        from recovery_helpers import backup_fixture, restore_fixture

        self.bind()
        self.verify()
        archive = backup_fixture(self)
        old_incarnation = self.store.incarnation
        restore_fixture(self, archive)
        with self.assertRaisesRegex(LocalError, "history_changed"):
            Repository(self.store).execute(
                self.owner,
                {
                    "version": 1,
                    "incarnation": old_incarnation,
                    "action": "inspect",
                    "args": {
                        "project": "p",
                        "repository": self.rid,
                        "snapshot": self.first["snapshot"],
                    },
                },
            )
        with self.assertRaisesRegex(LocalError, "snapshot_unchecked"):
            self.run_repo("inspect", repository=self.rid, snapshot=self.first["snapshot"])
        current = self.capture()
        self.assertEqual(current["snapshot"], self.first["snapshot"])
        self.assertEqual(self.query(bindings=[self.binding(current)])["status"], "blocked")

    def test_symbol_impact_has_authorized_endpoints_and_never_validates(self):
        inspection = self.run_repo("inspect", repository=self.rid, snapshot=self.first["snapshot"])
        symbol = inspection["files"]["a.py"]["symbols"][0]["id"]
        self.run_repo(
            "bind",
            revision=self.claim["revision"],
            repository=self.rid,
            snapshot=self.first["snapshot"],
            dependencies=[{"kind": "symbol", "target": symbol, "policy": "revalidate"}],
            impact="high",
            coverage_reviewed=True,
            expected_policy=0,
            evidence=self.obs["observation"],
        )
        preview = self.run_repo(
            "impact",
            repository=self.rid,
            snapshot=self.first["snapshot"],
            paths=["a.py"],
            limit=128,
        )
        ids = {node["id"] for node in preview["nodes"]}
        self.assertTrue(
            all(edge["source"] in ids and edge["target"] in ids for edge in preview["edges"])
        )
        self.assertIn(symbol, ids)
        self.assertEqual(self.query(bindings=[self.binding()])["status"], "blocked")

    def test_snapshot_verification_uses_persisted_authorization_instant(self):
        self.bind()
        clock = FakeClock()
        clock.utc = self.store.clock.utc_ms() + 1
        self.store.clock = clock
        start = clock.utc

        def cross_expiry(point):
            if point == "after_validation":
                clock.utc += 100
                clock.mono += 100

        self.store.fault = cross_expiry
        result = self.run_repo(
            "verify",
            revision=self.claim["revision"],
            snapshot=self.first["snapshot"],
            expected_policy=1,
            evidence=self.obs["observation"],
            passed=True,
            expires_ms=start + 50,
        )
        self.store.fault = lambda point: None
        self.assertEqual(result["evaluated_ms"], start)
        self.assertEqual(result["state"], "valid")
        self.assertEqual(self.query(bindings=[self.binding()])["status"], "blocked")

    def test_constraint_dependency_policy_needs_policy_authority(self):
        constraint = self.propose(
            self.body(
                "mandatory",
                kind="constraint",
                authority="project",
                rule_key="required",
                effect="require",
                rule_value="check",
            ),
            key="constraint",
        )
        self.review(constraint)
        reviewer = self.store.grant(self.owner, "reviewer", "p", ["read", "review", "attest"])
        with self.assertRaisesRegex(LocalError, "denied"):
            self.run_repo(
                "bind",
                credential=reviewer,
                revision=constraint["revision"],
                repository=self.rid,
                snapshot=self.first["snapshot"],
                dependencies=[{"kind": "file", "target": "a.py", "policy": "invalidate"}],
                impact="high",
                coverage_reviewed=True,
                expected_policy=0,
                evidence=self.obs["observation"],
            )

    def test_schema_five_upgrade_preserves_existing_memory(self):
        import sqlite3

        from memoryking.adapters.knowledge_store import Knowledge
        from memoryking.adapters.local_store import LocalStore

        self.store.close()
        with sqlite3.connect(self.data / "store.sqlite") as db:
            for table in (
                "repository_jobs",
                "code_validations",
                "code_policies",
                "repository_snapshots",
                "repository_clones",
                "repositories",
            ):
                db.execute("DROP TABLE " + table)
            historical_inventory(db, 5)
        self.store = LocalStore(self.data, self.control)
        self.k = Knowledge(self.store)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 9)
        self.assertEqual(self.query()["items"][0]["revision"], self.claim["revision"])
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM repositories").fetchone()[0], 0
        )


if __name__ == "__main__":
    unittest.main()
