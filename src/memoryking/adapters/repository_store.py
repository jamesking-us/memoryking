"""Authorized repository snapshots and dependency validation under the release gate."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator

from memoryking.adapters.local_files import private_file
from memoryking.adapters.local_store import RETRY_MS, LocalStore, uid
from memoryking.adapters.repository_git import GitSnapshot, canonical_remote
from memoryking.contracts.canonical import canonical_bytes, canonical_hash
from memoryking.domain.local import LocalError, Released
from memoryking.domain.selectors import Truth, bindings, path_valid

VALIDATOR = Draft202012Validator(
    json.loads(Path(__file__).with_name("repository.schema.json").read_text())
)


class Repository:
    def __init__(self, store: LocalStore):
        self.s, self.db = store, store.db

    def execute(self, credential: str, command: dict[str, Any]) -> Released:
        if not VALIDATOR.is_valid(command) or len(canonical_bytes(command)) > 131072:
            raise LocalError("invalid_request")
        if command["incarnation"] != self.s.incarnation:
            raise LocalError("history_changed")
        action, args = command["action"], command["args"]
        mutation = action not in {"inspect", "impact", "coverage", "pending", "history"}
        cap = {
            "register": "admin",
            "capture": "repository",
            "bind": "review",
            "verify": "attest",
            "revalidate": "repository",
        }.get(action, "read")
        # Git runs before the write transaction so the store lock never waits on the
        # repository; the commit below rechecks authorization and clone identity.
        snapshot = self.snapshot_outside(credential, args) if action == "capture" else None
        with self.s.transaction() as decision:
            at = decision.evaluated_ms
            who = self.s._auth(credential, cap, args["project"], at)
            self.s._auth(credential, "read", args["project"], at)
            if action == "bind":
                from memoryking.adapters.knowledge_store import Knowledge

                self.s._auth(credential, "attest", args["project"], at)
                Knowledge(self.s)._authority(
                    credential, self._claim(who, args["revision"], args["project"], at), at
                )
            if "repository" in args:
                self.visible(who, args["repository"], args["project"])
            lookup = (
                who["tenant"],
                self.s.incarnation,
                "repository-v1",
                who["principal"],
                action,
                command.get("key"),
            )
            existing = (
                self.db.execute(
                    "SELECT * FROM retries WHERE tenant=? AND incarnation=? AND namespace=? AND principal=? AND operation=? AND key=?",
                    lookup,
                ).fetchone()
                if mutation
                else None
            )
            if existing is not None:
                if existing["hash"] != canonical_hash(command):
                    raise LocalError("idempotency_conflict")
                if existing["result"] is None or at >= existing["expires"]:
                    raise LocalError("idempotency_result_expired")
                # Present visibility still applies to retries of claim-dependent operations.
                if "revision" in args:
                    self._claim(who, args["revision"], args["project"], at)
                result = json.loads(existing["result"])
            elif action == "register":
                result = self.register(who, args, at)
            elif action == "capture":
                assert snapshot is not None
                result = self.capture(who, args, at, snapshot)
            elif action == "bind":
                result = self.bind(who, args, at)
            elif action == "verify":
                result = self.verify(who, args, at)
            elif action == "revalidate":
                result = self.revalidate(who, args, at)
            elif action == "pending":
                result = {
                    "jobs": [
                        dict(row)
                        for row in self.db.execute(
                            "SELECT snapshot,generation,state FROM repository_jobs WHERE tenant=? AND incarnation=? AND repository=? AND state='pending' ORDER BY generation DESC LIMIT 1",
                            (who["tenant"], self.s.incarnation, args["repository"]),
                        )
                    ]
                }
            elif action == "history":
                result = self.history(who, args, at)
            elif action == "impact":
                result = self.impact(who, args, at)
            elif action == "coverage":
                result = self.coverage(who, args)
            else:
                snap = self.snapshot(who["tenant"], args["repository"], args["snapshot"])
                body = json.loads(snap["body"])
                files = sorted(body["files"].items())
                # The full manifest is retained internally; output has an explicit bound.
                result = {k: v for k, v in body.items() if k not in {"files", "ancestors"}}
                result.update(
                    files=dict(files[:32]),
                    files_total=len(files),
                    truncated=len(files) > 32,
                    generation=snap["generation"],
                    ancestry_complete=body["ancestry_complete"],
                )
            if mutation and existing is None:
                self.db.execute(
                    "INSERT INTO retries VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (*lookup, canonical_hash(command), json.dumps(result), None, at + RETRY_MS),
                )
            result.update(
                incarnation=self.s.incarnation,
                evaluated_ms=at,
                deadline_ms=decision.deadline_ms,
                receipt=uid("rcpt"),
            )
            if len(canonical_bytes(result)) > 262144:
                result = {
                    "status": "blocked",
                    "reason": "response_limit_exceeded",
                    "receipt": result["receipt"],
                    "incarnation": self.s.incarnation,
                }
            epochs = self.db.execute(
                "SELECT policy_epoch,purge_epoch FROM tenants WHERE tenant=?", (who["tenant"],)
            ).fetchone()
            self.db.execute(
                "INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    who["tenant"],
                    result["receipt"],
                    who["principal"],
                    self.s.incarnation,
                    at,
                    decision.deadline_ms,
                    *epochs,
                    canonical_hash(result),
                    "[]",
                ),
            )
            self.s.fault("after_validation")
        self.s._expose()
        return Released(canonical_bytes(result), decision, self.s.clock)

    def visible(self, who: dict[str, Any], repository: str, project: str) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT * FROM repositories WHERE tenant=? AND id=? AND project=?",
            (who["tenant"], repository, project),
        ).fetchone()
        if row is None or (
            row["readers"] != "null" and who["principal"] not in json.loads(row["readers"])
        ):
            raise LocalError("unavailable")
        return cast(sqlite3.Row, row)

    def snapshot(self, tenant: str, repository: str, snapshot: str) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT * FROM repository_snapshots WHERE tenant=? AND incarnation=? AND repository=? AND id=?",
            (tenant, self.s.incarnation, repository, snapshot),
        ).fetchone()
        if row is None:
            raise LocalError("snapshot_unchecked")
        return cast(sqlite3.Row, row)

    def register(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        remote = canonical_remote(args["remote"])
        root = GitSnapshot(Path(args["path"])).root
        if args["readers"] is not None and who["principal"] not in args["readers"]:
            raise LocalError("repository_audience_invalid")
        repo = self.db.execute(
            "SELECT * FROM repositories WHERE tenant=? AND project=? AND remote=?",
            (who["tenant"], args["project"], remote),
        ).fetchone()
        if repo is None:
            rid = uid("repo")
            self.db.execute(
                "INSERT INTO repositories(tenant,id,project,remote,readers) VALUES (?,?,?,?,?)",
                (who["tenant"], rid, args["project"], remote, json.dumps(args["readers"])),
            )
        else:
            rid = repo["id"]
            if json.loads(repo["readers"]) != args["readers"]:
                raise LocalError("repository_audience_conflict")
        identity = root.stat()
        existing = self.db.execute(
            "SELECT * FROM repository_clones WHERE tenant=? AND (path=? OR (repository=? AND alias=?))",
            (who["tenant"], str(root), rid, args["clone"]),
        ).fetchall()
        if existing:
            raise LocalError("clone_already_registered")
        self.db.execute(
            "INSERT INTO repository_clones VALUES (?,?,?,?,?,?)",
            (who["tenant"], rid, args["clone"], str(root), identity.st_dev, identity.st_ino),
        )
        position = self.s._event(who["tenant"], "repository.registered", rid, at, who["principal"])
        self.s._enqueue(who["tenant"], position, None, who["principal"])
        return {
            "repository": rid,
            "clone": args["clone"],
            "canonical_remote": remote,
            "position": position,
        }

    def _clone_root(self, who: dict[str, Any], args: dict[str, Any]) -> Path:
        clone = self.db.execute(
            "SELECT * FROM repository_clones WHERE tenant=? AND repository=? AND alias=?",
            (who["tenant"], args["repository"], args["clone"]),
        ).fetchone()
        if clone is None:
            raise LocalError("unavailable")
        root = Path(clone["path"])
        identity = root.lstat()
        if root.is_symlink() or (identity.st_dev, identity.st_ino) != (
            clone["device"],
            clone["inode"],
        ):
            raise LocalError("repository_clone_changed")
        return root

    def snapshot_outside(self, credential: str, args: dict[str, Any]) -> dict[str, Any]:
        """Authorize and locate the clone in a short transaction, then run Git unlocked."""
        with self.s.transaction() as decision:
            at = decision.evaluated_ms
            who = self.s._auth(credential, "repository", args["project"], at)
            self.s._auth(credential, "read", args["project"], at)
            self.visible(who, args["repository"], args["project"])
            root = self._clone_root(who, args)
        return GitSnapshot(root).capture(args["repository"], args["environment"])

    def capture(
        self, who: dict[str, Any], args: dict[str, Any], at: int, body: dict[str, Any]
    ) -> dict[str, Any]:
        self._clone_root(who, args)  # The clone must still be the registered one at commit.
        raw = canonical_bytes(body)
        if len(raw) > 4 * 1024 * 1024:
            raise LocalError("repository_projection_limit")
        current = self.visible(who, args["repository"], args["project"])
        if (
            current["current_incarnation"] == self.s.incarnation
            and current["current_snapshot"] == body["snapshot"]
        ):
            existing = self.snapshot(who["tenant"], args["repository"], body["snapshot"])
            return {
                "snapshot": body["snapshot"],
                "commit": body["commit"],
                "dirty": body["dirty"],
                "generation": current["generation"],
                "position": existing["position"],
                "status": "captured",
                "validation": "per_claim",
                "dirty_files": [
                    {
                        "path": p,
                        "hash": body["files"][p]["hash"],
                        "coverage": body["files"][p]["coverage"],
                    }
                    for p in body["dirty_paths"]
                ],
            }
        self.db.execute(
            "UPDATE repositories SET current_incarnation=?,current_snapshot=? WHERE tenant=? AND id=?",
            (self.s.incarnation, body["snapshot"], who["tenant"], args["repository"]),
        )
        self.db.execute(
            "UPDATE repositories SET generation=generation+1 WHERE tenant=? AND id=?",
            (who["tenant"], args["repository"]),
        )
        gen = self.visible(who, args["repository"], args["project"])["generation"]
        position = self.s._event(
            who["tenant"], "repository.barrier", args["repository"], at, who["principal"]
        )
        self.db.execute(
            "INSERT INTO repository_snapshots VALUES (?,?,?,?,?,?,?) ON CONFLICT(tenant,incarnation,repository,id) DO UPDATE SET generation=excluded.generation,position=excluded.position",
            (
                who["tenant"],
                self.s.incarnation,
                args["repository"],
                body["snapshot"],
                gen,
                position,
                raw.decode(),
            ),
        )
        self.db.execute(
            "UPDATE repository_jobs SET state='superseded' WHERE tenant=? AND incarnation=? AND repository=? AND state='pending'",
            (who["tenant"], self.s.incarnation, args["repository"]),
        )
        self.db.execute(
            "INSERT INTO repository_jobs VALUES (?,?,?,?,?,?)",
            (
                who["tenant"],
                self.s.incarnation,
                args["repository"],
                body["snapshot"],
                gen,
                "pending",
            ),
        )
        self.s._enqueue(who["tenant"], position, None, who["principal"])
        return {
            "snapshot": body["snapshot"],
            "commit": body["commit"],
            "dirty": body["dirty"],
            "generation": gen,
            "position": position,
            "status": "captured",
            "validation": "per_claim",
            "dirty_files": [
                {
                    "path": path,
                    "hash": body["files"][path]["hash"],
                    "coverage": body["files"][path]["coverage"],
                }
                for path in body["dirty_paths"]
            ],
        }

    def _claim(self, who: dict[str, Any], revision: str, project: str, at: int) -> sqlite3.Row:
        from memoryking.adapters.knowledge_store import Knowledge

        k = Knowledge(self.s)
        row = k._revision(who["tenant"], revision, project)
        state, quarantine = k._state(
            who["tenant"],
            revision,
            self.db.execute(
                "SELECT position FROM tenants WHERE tenant=?", (who["tenant"],)
            ).fetchone()[0],
        )
        if state != "accepted" or quarantine or not k._available(who, row, at):
            raise LocalError("unavailable")
        return row

    def _evidence(self, who: dict[str, Any], evidence: str, at: int) -> None:
        from memoryking.adapters.knowledge_store import Knowledge

        row = self.db.execute(
            "SELECT * FROM observations WHERE tenant=? AND id=?", (who["tenant"], evidence)
        ).fetchone()
        if row is None or not self.s._visible(who, row, at):
            raise LocalError("evidence_unavailable")
        path = self.s.root / "blobs" / who["tenant"] / row["blob"]
        private_file(path)
        with path.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise LocalError("evidence_unavailable")
        Knowledge(self.s)._support(
            who,
            [
                {
                    "observation": evidence,
                    "start_byte": 0,
                    "end_byte": len(raw),
                    "content_hash": row["blob"],
                }
            ],
            at,
        )

    @staticmethod
    def dependency_hash(body: dict[str, Any], dep: dict[str, Any]) -> str | None:
        if dep["kind"] == "file":
            return cast(str | None, body["files"].get(dep["target"], {}).get("hash"))
        for file in body["files"].values():
            if file.get("coverage") == "python_syntax" and not file.get("truncated"):
                for symbol in file["symbols"]:
                    if symbol["id"] == dep["target"]:
                        return cast(str, symbol["hash"])
        return None

    def bind(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        row = self._claim(who, args["revision"], args["project"], at)
        self._evidence(who, args["evidence"], at)
        old = self.db.execute(
            "SELECT * FROM code_policies WHERE tenant=? AND revision=?", (who["tenant"], row["id"])
        ).fetchone()
        version = old["version"] if old else 0
        if args["expected_policy"] != version:
            raise LocalError("dependency_policy_conflict")
        if not args["dependencies"] or len(
            {(d["kind"], d["target"]) for d in args["dependencies"]}
        ) != len(args["dependencies"]):
            raise LocalError("dependency_coverage_incomplete")
        body = json.loads(
            self.snapshot(who["tenant"], args["repository"], args["snapshot"])["body"]
        )
        dependencies = []
        for dep in args["dependencies"]:
            if dep["kind"] == "file" and not path_valid(dep["target"]):
                raise LocalError("dependency_unsupported")
            digest = self.dependency_hash(body, dep)
            if digest is None:
                raise LocalError("dependency_coverage_incomplete")
            if dep["policy"] == "rank_penalty" and (
                args["impact"] != "low" or row["kind"] == "constraint"
            ):
                raise LocalError("dependency_policy_unsafe")
            dependencies.append({**dep, "hash": digest})
        impact = "high" if row["kind"] == "constraint" else args["impact"]
        if old and old["impact"] == "high" and impact != "high":
            raise LocalError("impact_downgrade_requires_new_revision")
        self.db.execute(
            "INSERT OR REPLACE INTO code_policies VALUES (?,?,?,?,?,?,?,?,?)",
            (
                who["tenant"],
                row["id"],
                args["repository"],
                args["snapshot"],
                version + 1,
                impact,
                json.dumps(dependencies),
                args["evidence"],
                who["principal"],
            ),
        )
        position = self.s._event(
            who["tenant"], "repository.dependency_policy", row["id"], at, who["principal"]
        )
        self.s._enqueue(who["tenant"], position, None, who["principal"])
        return {
            "revision": row["id"],
            "policy_version": version + 1,
            "impact": impact,
            "status": "unchecked",
            "position": position,
        }

    def verify(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        row = self._claim(who, args["revision"], args["project"], at)
        policy = self.db.execute(
            "SELECT * FROM code_policies WHERE tenant=? AND revision=?", (who["tenant"], row["id"])
        ).fetchone()
        if policy is None or policy["version"] != args["expected_policy"]:
            raise LocalError("dependency_policy_conflict")
        self.visible(who, policy["repository"], args["project"])
        snap = self.snapshot(who["tenant"], policy["repository"], args["snapshot"])
        body = json.loads(snap["body"])
        self._evidence(who, args["evidence"], at)
        if args["expires_ms"] <= at:
            raise LocalError("verification_expired")
        # A changed dependency needs a reviewed policy revision first. An attributed
        # verification never silently rebases the dependency declaration.
        if any(
            self.dependency_hash(body, dep) != dep["hash"]
            for dep in json.loads(policy["dependencies"])
        ):
            raise LocalError("dependency_policy_stale")
        state = "valid" if args["passed"] else "invalid"
        self.db.execute(
            "INSERT OR REPLACE INTO code_validations VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                who["tenant"],
                self.s.incarnation,
                row["id"],
                args["snapshot"],
                policy["version"],
                state,
                args["evidence"],
                args["expires_ms"],
                "attributed_snapshot_verification",
                snap["generation"],
            ),
        )
        position = self.s._event(
            who["tenant"], "repository.verified", row["id"], at, who["principal"]
        )
        self.s._enqueue(who["tenant"], position, None, who["principal"])
        return {
            "revision": row["id"],
            "snapshot": args["snapshot"],
            "state": state,
            "environment": body["environment"],
            "position": position,
        }

    def revalidate(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        repo = self.visible(who, args["repository"], args["project"])
        snap = self.snapshot(who["tenant"], repo["id"], args["snapshot"])
        if (
            repo["generation"] != args["expected_generation"]
            or snap["generation"] != args["expected_generation"]
        ):
            raise LocalError("repository_generation_conflict")
        body = json.loads(snap["body"])
        policies = self.db.execute(
            "SELECT * FROM code_policies WHERE tenant=? AND repository=? ORDER BY revision LIMIT 257",
            (who["tenant"], repo["id"]),
        ).fetchall()
        if len(policies) > 256:
            raise LocalError("revalidation_limit")
        results = []
        for policy in policies:
            try:
                self._claim(who, policy["revision"], args["project"], at)
                self._evidence(who, policy["evidence"], at)
            except LocalError:
                continue
            current = self.db.execute(
                "SELECT * FROM code_validations WHERE tenant=? AND incarnation=? AND revision=? AND snapshot=? AND policy=?",
                (
                    who["tenant"],
                    self.s.incarnation,
                    policy["revision"],
                    snap["id"],
                    policy["version"],
                ),
            ).fetchone()
            if current is not None and current["reason"] == "attributed_snapshot_verification":
                # A failed explicit verification cannot be replaced by hash carry-forward.
                results.append(
                    {
                        "revision": policy["revision"],
                        "state": "unchecked"
                        if current["state"] == "valid" and current["expires"] <= at
                        else current["state"],
                        "expires_ms": current["expires"],
                    }
                )
                continue
            try:
                origin = self.snapshot(who["tenant"], repo["id"], policy["origin"])
                original = json.loads(origin["body"])
            except LocalError:
                original = None
            prior = self.db.execute(
                "SELECT * FROM code_validations WHERE tenant=? AND incarnation=? AND revision=? AND snapshot=? AND policy=?",
                (
                    who["tenant"],
                    self.s.incarnation,
                    policy["revision"],
                    policy["origin"],
                    policy["version"],
                ),
            ).fetchone()
            dependencies = json.loads(policy["dependencies"])
            values = [(dep, self.dependency_hash(body, dep)) for dep in dependencies]
            changed = [dep for dep, digest in values if digest != dep["hash"]]
            reason, state = "verification_unavailable", "unchecked"
            if any(digest is None for _, digest in values):
                reason = "dependency_missing"
            elif changed:
                state = (
                    "invalid"
                    if any(dep["policy"] == "invalidate" for dep in changed)
                    else "unchecked"
                )
                reason = "dependency_changed"
            elif original is None:
                reason = "origin_snapshot_unavailable"
            elif original["environment"] != body["environment"]:
                reason = "environment_changed"
            elif original["commit"] not in body["ancestors"]:
                state = "invalid" if body["ancestry_complete"] else "unchecked"
                reason = "lineage_mismatch" if state == "invalid" else "ancestry_unknown"
            elif prior is not None and prior["state"] == "valid" and prior["expires"] > at:
                try:
                    self._evidence(who, prior["evidence"], at)
                    reason, state = "trusted_dependency_comparison", "valid"
                except (LocalError, OSError):
                    pass
            evidence = prior["evidence"] if prior is not None else policy["evidence"]
            expires = prior["expires"] if prior is not None else at
            self.db.execute(
                "INSERT OR REPLACE INTO code_validations VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    who["tenant"],
                    self.s.incarnation,
                    policy["revision"],
                    snap["id"],
                    policy["version"],
                    state,
                    evidence,
                    expires,
                    reason,
                    snap["generation"],
                ),
            )
            results.append({"revision": policy["revision"], "state": state, "reason": reason})
        self.db.execute(
            "UPDATE repository_jobs SET state='evaluated' WHERE tenant=? AND incarnation=? AND repository=? AND snapshot=? AND generation=?",
            (who["tenant"], self.s.incarnation, repo["id"], snap["id"], snap["generation"]),
        )
        position = self.s._event(
            who["tenant"], "repository.revalidated", repo["id"], at, who["principal"]
        )
        self.s._enqueue(who["tenant"], position, None, who["principal"])
        return {
            "snapshot": snap["id"],
            "generation": snap["generation"],
            "position": position,
            "results": results,
            "checks_executed": False,
        }

    def eligibility(
        self, who: dict[str, Any], row: sqlite3.Row, binding: dict[str, Any], at: int
    ) -> Truth:
        policy = self.db.execute(
            "SELECT * FROM code_policies WHERE tenant=? AND revision=?", (who["tenant"], row["id"])
        ).fetchone()
        if policy is None:
            # A selector alone is not a claim of code verification. Existing
            # repository-independent rules retain P03 applicability semantics.
            return "true"
        if (
            binding.get("repository_id") is not None
            and binding["repository_id"] != policy["repository"]
        ):
            return "false"
        if not all(
            isinstance(binding.get(k), str) and binding[k]
            for k in ("snapshot_id", "repository_id", "commit", "environment")
        ):
            return "unknown"
        try:
            self.visible(who, policy["repository"], row["project"])
            snap = self.snapshot(who["tenant"], policy["repository"], binding["snapshot_id"])
            body = json.loads(snap["body"])
            if (
                binding.get("commit") != body["commit"]
                or binding.get("environment") != body["environment"]
            ):
                return "unknown"
            self._evidence(who, policy["evidence"], at)
            validation = self.db.execute(
                "SELECT * FROM code_validations WHERE tenant=? AND incarnation=? AND revision=? AND snapshot=? AND policy=?",
                (who["tenant"], self.s.incarnation, row["id"], snap["id"], policy["version"]),
            ).fetchone()
            if validation is None or validation["state"] == "unchecked":
                validation = self.carry_forward(who, policy, snap, body, at)
                if validation is None:
                    return "unknown"
            if validation["state"] == "invalid":
                return "false"
            if validation["expires"] <= at:
                return "unknown"
            self._evidence(who, validation["evidence"], at)
            return "true"
        except (LocalError, OSError):
            return "unknown"

    def carry_forward(
        self,
        who: dict[str, Any],
        policy: sqlite3.Row,
        snap: sqlite3.Row,
        body: dict[str, Any],
        at: int,
    ) -> sqlite3.Row | None:
        """Cheap foreground proof for unaffected guidance while workers are paused.

        The snapshot and complete declared dependency hashes came from the trusted
        adapter. No positive proof is inferred from ancestry alone or unseen data.
        """
        try:
            origin = json.loads(
                self.snapshot(who["tenant"], policy["repository"], policy["origin"])["body"]
            )
            prior = self.db.execute(
                "SELECT * FROM code_validations WHERE tenant=? AND incarnation=? AND revision=? AND snapshot=? AND policy=?",
                (
                    who["tenant"],
                    self.s.incarnation,
                    policy["revision"],
                    policy["origin"],
                    policy["version"],
                ),
            ).fetchone()
            if (
                prior is None
                or prior["state"] != "valid"
                or prior["expires"] <= at
                or origin["environment"] != body["environment"]
                or origin["commit"] not in body["ancestors"]
            ):
                return None
            if any(
                self.dependency_hash(body, dep) != dep["hash"]
                for dep in json.loads(policy["dependencies"])
            ):
                return None
            self._evidence(who, prior["evidence"], at)
            self.db.execute(
                "INSERT OR REPLACE INTO code_validations VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    who["tenant"],
                    self.s.incarnation,
                    policy["revision"],
                    snap["id"],
                    policy["version"],
                    "valid",
                    prior["evidence"],
                    prior["expires"],
                    "trusted_dependency_comparison",
                    snap["generation"],
                ),
            )
            return cast(
                sqlite3.Row,
                self.db.execute(
                    "SELECT * FROM code_validations WHERE tenant=? AND incarnation=? AND revision=? AND snapshot=? AND policy=?",
                    (
                        who["tenant"],
                        self.s.incarnation,
                        policy["revision"],
                        snap["id"],
                        policy["version"],
                    ),
                ).fetchone(),
            )
        except (LocalError, OSError):
            return None

    def ancestry(
        self,
        who: dict[str, Any],
        project: str,
        repository: str,
        ancestor: str,
        commit: str,
        snapshot_id: str | None = None,
    ) -> Truth:
        try:
            self.visible(who, repository, project)
        except LocalError:
            return "unknown"
        if snapshot_id:
            try:
                body = json.loads(self.snapshot(who["tenant"], repository, snapshot_id)["body"])
                if body["commit"] != commit:
                    return "unknown"
                return (
                    "true"
                    if ancestor in body["ancestors"]
                    else "false"
                    if body["ancestry_complete"]
                    else "unknown"
                )
            except LocalError:
                return "unknown"
        # Evidence is bounded and captured by the trusted adapter. Request fields
        # cannot inject a parent list or an assertion of graph completeness.
        rows = self.db.execute(
            "SELECT body FROM repository_snapshots WHERE tenant=? AND incarnation=? AND repository=? ORDER BY position DESC LIMIT 64",
            (who["tenant"], self.s.incarnation, repository),
        ).fetchall()
        complete = False
        for row in rows:
            body = json.loads(row["body"])
            if body["commit"] != commit:
                continue
            if ancestor in body["ancestors"]:
                return "true"
            complete |= body["ancestry_complete"]
        return "false" if complete else "unknown"

    def impact(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        snap = self.snapshot(who["tenant"], args["repository"], args["snapshot"])
        body = json.loads(snap["body"])
        if any(not path_valid(p) for p in args["paths"]):
            raise LocalError("invalid_binding")
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        for path in args["paths"]:
            file = body["files"].get(path)
            nodes.append(
                {
                    "id": path,
                    "kind": "file",
                    "coverage": file["coverage"] if file else "missing",
                    "hash": file["hash"] if file else None,
                }
            )
        truncated = False
        # Authorization filters all endpoints before counting or returning edges.
        policies = self.db.execute(
            "SELECT p.* FROM code_policies p JOIN revisions r ON r.tenant=p.tenant AND r.id=p.revision JOIN observations o ON o.tenant=p.tenant AND o.id=p.evidence WHERE p.tenant=? AND p.repository=? AND r.purged=0 AND r.body IS NOT NULL AND (json_type(r.body,'$.readers')='null' OR EXISTS(SELECT 1 FROM json_each(r.body,'$.readers') WHERE value=?)) AND o.deleted=0 AND (o.readers='null' OR EXISTS(SELECT 1 FROM json_each(o.readers) WHERE value=?)) ORDER BY p.revision LIMIT 4096",
            (who["tenant"], args["repository"], who["principal"], who["principal"]),
        ).fetchall()
        for policy in policies:
            # Resolve complete current source authority before traversing declared
            # relationships or contributing to any count/truncation indicator.
            try:
                self._claim(who, policy["revision"], args["project"], at)
                self._evidence(who, policy["evidence"], at)
            except (LocalError, OSError):
                continue
            deps = [
                dep
                for dep in json.loads(policy["dependencies"])
                if any(
                    dep["target"] == p
                    or (dep["kind"] == "symbol" and dep["target"].startswith(p + ":"))
                    for p in args["paths"]
                )
            ]
            if not deps:
                continue
            target_nodes = [
                {"id": dep["target"], "kind": dep["kind"]}
                for dep in deps
                if dep["target"] not in {n["id"] for n in nodes}
            ]
            if (
                len(nodes) + 1 + len(target_nodes) > args["limit"]
                or len(edges) + len(deps) > args["limit"]
            ):
                truncated = True
                continue
            nodes.extend(target_nodes)
            nodes.append({"id": policy["revision"], "kind": "claim", "impact": policy["impact"]})
            for dep in deps:
                digest = self.dependency_hash(body, dep)
                edges.append(
                    {
                        "source": policy["revision"],
                        "target": dep["target"],
                        "kind": "depends_on_" + dep["kind"],
                        "change_policy": dep["policy"],
                        "status": "missing"
                        if digest is None
                        else "known_affected"
                        if digest != dep["hash"]
                        else "unchanged",
                        "needs_revalidation": digest != dep["hash"],
                    }
                )
        return {
            "status": "partial",
            "snapshot": snap["id"],
            "generation": snap["generation"],
            "nodes": nodes[: args["limit"]],
            "edges": edges,
            "truncated": truncated or len(nodes) > args["limit"],
            "missing_relationships": "dynamic_undeclared_or_bounded_scan_unknown",
            "candidate_scan_limit": 4096,
            "checks_executed": False,
            "mutation": False,
        }

    def coverage(self, who: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        snap = self.snapshot(who["tenant"], args["repository"], args["snapshot"])
        body = json.loads(snap["body"])
        points = bindings(args["bindings"])
        if not args["paths"] or any(not path_valid(p) for p in args["paths"]):
            raise LocalError("binding_coverage_incomplete")
        covered = {
            b.get("path")
            for b in points
            if b.get("repository_id") == args["repository"]
            and b.get("snapshot_id") == snap["id"]
            and b.get("commit") == body["commit"]
            and b.get("environment") == body["environment"]
        }
        missing = sorted(set(args["paths"]) - covered)
        return {
            "status": "blocked" if missing else "complete",
            "reason": "binding_coverage_incomplete" if missing else None,
            "missing_paths": missing,
            "snapshot": snap["id"],
            "declared_scope_only": True,
        }

    def proof(
        self, who: dict[str, Any], row: sqlite3.Row, binding: dict[str, Any], at: int
    ) -> dict[str, Any] | None:
        policy = self.db.execute(
            "SELECT * FROM code_policies WHERE tenant=? AND revision=?", (who["tenant"], row["id"])
        ).fetchone()
        if policy is None or self.eligibility(who, row, binding, at) != "true":
            return None
        snap = self.snapshot(who["tenant"], policy["repository"], binding["snapshot_id"])
        validation = self.db.execute(
            "SELECT * FROM code_validations WHERE tenant=? AND incarnation=? AND revision=? AND snapshot=? AND policy=?",
            (who["tenant"], self.s.incarnation, row["id"], snap["id"], policy["version"]),
        ).fetchone()
        return {
            "dependency": f"{row['id']}|{snap['id']}|{policy['version']}",
            "repository": policy["repository"],
            "snapshot": snap["id"],
            "environment": binding["environment"],
            "commit": binding["commit"],
            "policy_version": policy["version"],
            "generation": snap["generation"],
            "method": validation["reason"],
            "expires_ms": validation["expires"],
            "evidence": sorted({policy["evidence"], validation["evidence"]}),
        }

    def verify_proof(self, who: dict[str, Any], dependency: str, at: int) -> None:
        try:
            revision, snapshot, version = dependency.split("|")
            policy = self.db.execute(
                "SELECT * FROM code_policies WHERE tenant=? AND revision=? AND version=?",
                (who["tenant"], revision, int(version)),
            ).fetchone()
            if policy is None:
                raise LocalError("replay_denied")
            from memoryking.adapters.knowledge_store import Knowledge

            row = Knowledge(self.s)._revision(who["tenant"], revision)
            body = json.loads(self.snapshot(who["tenant"], policy["repository"], snapshot)["body"])
            binding = {
                "repository_id": policy["repository"],
                "snapshot_id": snapshot,
                "commit": body["commit"],
                "environment": body["environment"],
            }
            if self.eligibility(who, row, binding, at) != "true":
                raise LocalError("replay_denied")
        except (ValueError, OSError, LocalError) as exc:
            raise LocalError("replay_denied") from exc

    def history(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        row = self._claim(who, args["revision"], args["project"], at)
        policy = self.db.execute(
            "SELECT * FROM code_policies WHERE tenant=? AND revision=?", (who["tenant"], row["id"])
        ).fetchone()
        if policy is None:
            raise LocalError("dependency_policy_missing")
        self.visible(who, policy["repository"], args["project"])
        self._evidence(who, policy["evidence"], at)
        origin = json.loads(
            self.snapshot(who["tenant"], policy["repository"], policy["origin"])["body"]
        )
        target = json.loads(
            self.snapshot(who["tenant"], policy["repository"], args["snapshot"])["body"]
        )
        return {
            "status": "partial",
            "authority": "historical_analogy_not_current_guidance",
            "revision": row["id"],
            "text": json.loads(row["body"])["text"],
            "origin_snapshot": origin["snapshot"],
            "origin_commit": origin["commit"],
            "requested_snapshot": target["snapshot"],
            "requested_commit": target["commit"],
            "verified_current": False,
            "checks_executed": False,
        }
