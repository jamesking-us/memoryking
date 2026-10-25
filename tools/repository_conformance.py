"""Actual P06 adapter scenarios over isolated Git data and the supplied SQLite store."""

import hashlib
import json
import subprocess

from knowledge_demo import body

from memoryking.adapters.knowledge_store import Knowledge
from memoryking.adapters.repository_store import Repository


def exercise(store, credential, project, root, operation, arguments):
    counter = 0

    def command(adapter, action, **args):
        nonlocal counter
        counter += 1
        value = {
            "version": 1,
            "incarnation": store.incarnation,
            "action": action,
            "args": {"project": project, **args},
        }
        if action not in {
            "query",
            "constraints",
            "inspect",
            "impact",
            "coverage",
            "pending",
            "history",
        }:
            value["key"] = f"repo-scenario-{counter}"
        out = []
        adapter.execute(credential, value).emit(out.append)
        return json.loads(out[0])

    repo = Repository(store)
    knowledge = Knowledge(store)

    def git(path, *args):
        return (
            subprocess.check_output(["git", "-C", str(path), *args], stderr=subprocess.DEVNULL)
            .decode()
            .strip()
        )

    def setup(name):
        path = root / name
        path.mkdir()
        git(path, "init", "-q")
        git(path, "config", "user.name", "Fixture")
        git(path, "config", "user.email", "fixture@example.invalid")
        (path / "a.py").write_text("value = 1\n")
        git(path, "add", ".")
        git(path, "commit", "-qm", "Initial")
        rid = command(
            repo, "register", remote="local:" + name, clone="first", path=str(path), readers=None
        )["repository"]
        snap = command(repo, "capture", repository=rid, clone="first", environment="fixture")
        return path, rid, snap

    namespace = store.namespace(credential, project)
    raw = "Repository fixture observed by explicit manual review"
    obs = store.capture(
        credential,
        namespace,
        "repo-evidence",
        {
            "version": 1,
            "project": project,
            "source": "repository-scenario",
            "source_event": "repo-evidence",
            "text": raw,
            "readers": None,
            "classification": "internal",
            "observed_at_ms": store.clock.utc_ms(),
        },
    )["observation"]
    span = {
        "observation": obs,
        "content_hash": hashlib.sha256(raw.encode()).hexdigest(),
        "start_byte": 0,
        "end_byte": len(raw),
    }

    def claim(name, **kw):
        row = command(
            knowledge,
            "propose",
            logical_key=name,
            expected_version=0,
            body={**body(span, name), **kw},
        )
        command(
            knowledge,
            "review",
            revision=row["revision"],
            expected_version=row["family_version"],
            decision="accept",
            reason="reviewed_fixture",
            breadth_reviewed=True,
        )
        return row["revision"]

    def bind(revision, rid, snapshot):
        command(
            repo,
            "bind",
            revision=revision,
            repository=rid,
            snapshot=snapshot["snapshot"],
            dependencies=[{"kind": "file", "target": "a.py", "policy": "invalidate"}],
            impact="high",
            coverage_reviewed=True,
            expected_policy=0,
            evidence=obs,
        )
        command(
            repo,
            "verify",
            revision=revision,
            snapshot=snapshot["snapshot"],
            expected_policy=1,
            evidence=obs,
            passed=True,
            expires_ms=store.clock.utc_ms() + 600000,
        )

    def point(rid, snap):
        return {
            "repository_id": rid,
            "snapshot_id": snap["snapshot"],
            "commit": snap["commit"],
            "environment": "fixture",
            "path": "a.py",
        }

    path, rid, initial = setup("primary")
    if operation == "compile_current_context":
        base = initial["commit"]
        git(path, "checkout", "-qb", arguments["branch"])
        (path / "branch").write_text("branch")
        git(path, "add", ".")
        git(path, "commit", "-qm", "Branch")
        branch = command(repo, "capture", repository=rid, clone="first", environment="fixture")
        branch_claim = claim(
            "branch",
            selector={
                "selector_version": "1",
                "any_of": [
                    {
                        "all_of": [
                            {"field": "repository_id", "op": "eq", "value": rid},
                            {"field": "commit", "op": "descendant_of", "value": branch["commit"]},
                        ]
                    }
                ],
            },
        )
        now = store.clock.utc_ms()
        future = claim(
            "future", valid_start=now + arguments["future_valid_from_ms"] - arguments["now_ms"]
        )
        git(path, "checkout", "-qb", "unrelated", base)
        (path / "other").write_text("other")
        git(path, "add", ".")
        git(path, "commit", "-qm", "Other")
        target = command(repo, "capture", repository=rid, clone="first", environment="fixture")
        rows = command(knowledge, "query", bindings=[point(rid, target)], valid_ms=now)["items"]
        selected = {r["revision"] for r in rows}
        return {
            "future_claim_selected": future in selected,
            "unrelated_branch_selected": branch_claim in selected,
        }
    dangerous = claim("high-impact")
    bind(dangerous, rid, initial)
    # A separate, explicitly declared repository scope is unchanged and already
    # validated. No unknown primary snapshot can confer validity on that scope.
    _, independent_id, independent_snap = setup("independent")
    independent = claim("independent")
    bind(independent, independent_id, independent_snap)
    (path / "a.py").write_text("value = 9\n")
    changed = command(repo, "capture", repository=rid, clone="first", environment="fixture")
    checks = []
    for name in arguments["snapshots"]:
        target = changed if name == "changed" else {**changed, "snapshot": "snap_never_captured"}
        rows = command(
            knowledge,
            "query",
            bindings=[point(rid, target), point(independent_id, independent_snap)],
        )["items"]
        selected = {r["revision"] for r in rows}
        checks.append((dangerous in selected, independent in selected))
    return {
        "stale_high_impact_confirmed": any(stale for stale, _ in checks),
        "unrelated_available": all(available for _, available in checks),
    }
