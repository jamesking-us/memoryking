"""Disposable actual Git/Unix SDK/worker/harness workflow; no provider or repository execution."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from memoryking.adapters.local_ipc import Server, request
from memoryking.adapters.local_store import LocalStore
from memoryking.harness import LocalFileTool, ReferenceHarness
from memoryking.sdk import LocalClient

ROOT = Path(__file__).resolve().parents[1]


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="mk-repo-", dir="/tmp") as directory:
        root = Path(directory)
        code = root / "code"
        code.mkdir(mode=0o700)

        def git(*args: str) -> None:
            subprocess.run(["git", "-C", str(code), *args], check=True, capture_output=True)

        git("init", "-q")
        git("config", "user.name", "Disposable fixture")
        git("config", "user.email", "fixture@example.invalid")
        (code / "rule.py").write_text("timeout = 30\n")
        (code / "other.py").write_text("other = 1\n")
        for path in code.glob("*.py"):
            path.chmod(0o600)
        git("add", ".")
        git("commit", "-qm", "Initial data fixture")
        credential = LocalStore.initialize(root / "store", root / "control")
        with LocalStore(root / "store", root / "control") as store:
            with Server(store) as server:
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                harness = None
                try:
                    client = LocalClient(server.path, credential, "demo")
                    sequence = 0

                    def repo(action: str, **args: object) -> dict:
                        nonlocal sequence
                        sequence += 1
                        command = {
                            "version": 1,
                            "incarnation": store.incarnation,
                            "action": action,
                            "args": {"project": "demo", **args},
                        }
                        if action not in {"inspect", "impact", "coverage", "history", "pending"}:
                            command["key"] = "demo" + str(sequence)
                        return client.repository(command)

                    rid = repo(
                        "register", remote="local:demo", clone="first", path=str(code), readers=None
                    )["repository"]

                    def capture() -> dict:
                        return repo(
                            "capture", repository=rid, clone="first", environment="demo-python312"
                        )

                    first = capture()
                    observation = (
                        "Reviewed fixture: timeout = 30; manual review only, no tests executed."
                    )
                    evidence = client.capture(
                        "review-source",
                        {
                            "version": 1,
                            "project": "demo",
                            "source": "repository-demo",
                            "source_event": "review-source",
                            "text": observation,
                            "readers": None,
                            "classification": "internal",
                            "observed_at_ms": store.clock.utc_ms(),
                        },
                    )["observation"]
                    span = {
                        "observation": evidence,
                        "start_byte": 0,
                        "end_byte": len(observation.encode()),
                        "content_hash": hashlib.sha256(observation.encode()).hexdigest(),
                    }

                    def knowledge(action: str, **args: object) -> dict:
                        nonlocal sequence
                        sequence += 1
                        command = {
                            "version": 1,
                            "incarnation": store.incarnation,
                            "action": action,
                            "args": {"project": "demo", **args},
                        }
                        if action in {"propose", "review"}:
                            command["key"] = "knowledge" + str(sequence)
                        return request(
                            server.path, credential.read_text(), "knowledge", {"command": command}
                        )

                    claim = knowledge(
                        "propose",
                        logical_key="timeout",
                        expected_version=0,
                        body={
                            "text": "The reviewed fixture uses timeout = 30",
                            "kind": "knowledge",
                            "selector": {
                                "selector_version": "1",
                                "any_of": [{"all_of": [{"op": "any"}]}],
                            },
                            "valid_start": None,
                            "valid_end": None,
                            "task": None,
                            "run": None,
                            "readers": None,
                            "evidence": [span],
                            "authority": None,
                            "rule_key": None,
                            "effect": None,
                            "rule_value": None,
                            "provenance": {},
                        },
                    )
                    knowledge(
                        "review",
                        revision=claim["revision"],
                        expected_version=claim["family_version"],
                        decision="accept",
                        reason="fixture_reviewed",
                        breadth_reviewed=True,
                    )
                    repo(
                        "bind",
                        revision=claim["revision"],
                        repository=rid,
                        snapshot=first["snapshot"],
                        dependencies=[
                            {"kind": "file", "target": "rule.py", "policy": "invalidate"}
                        ],
                        impact="high",
                        coverage_reviewed=True,
                        expected_policy=0,
                        evidence=evidence,
                    )
                    repo(
                        "verify",
                        revision=claim["revision"],
                        snapshot=first["snapshot"],
                        expected_policy=1,
                        evidence=evidence,
                        passed=True,
                        expires_ms=store.clock.utc_ms() + 600000,
                    )

                    def query(snapshot: dict) -> dict:
                        return knowledge(
                            "query",
                            bindings=[
                                {
                                    "repository_id": rid,
                                    "snapshot_id": snapshot["snapshot"],
                                    "commit": snapshot["commit"],
                                    "environment": "demo-python312",
                                    "path": "rule.py",
                                }
                            ],
                        )

                    assert query(first)["status"] == "complete"
                    (code / "other.py").write_text("other = 2\n")
                    second = capture()
                    paused = query(second)
                    assert paused["status"] == "complete" and len(paused["items"]) == 1
                    worker = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "memoryking.repository_worker",
                            "--socket",
                            str(server.path),
                            "--credential",
                            str(credential),
                            "--project",
                            "demo",
                            "--repository",
                            rid,
                            "--once",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    assert worker.returncode == 0, worker.stderr
                    assert json.loads(worker.stdout)["results"][0]["state"] == "valid"
                    assert query(second)["status"] == "complete"
                    (code / "rule.py").write_text("timeout = 90\n")
                    third = capture()
                    assert query(third)["status"] == "blocked"
                    preview = repo(
                        "impact",
                        repository=rid,
                        snapshot=third["snapshot"],
                        paths=["rule.py"],
                        limit=128,
                    )
                    assert preview["edges"][0]["status"] == "known_affected"
                    history = repo(
                        "history", revision=claim["revision"], snapshot=third["snapshot"]
                    )
                    assert history["verified_current"] is False
                    # The reference harness checkpoints the declared scope. Dirty
                    # content still needs durable artifacts for a full resume.
                    state = json.loads((ROOT / "fixtures/contracts/checkpoint.json").read_text())
                    harness = ReferenceHarness(
                        client,
                        root / "journal",
                        LocalFileTool(code),
                        task="repository-demo",
                        state=state,
                    )
                    refreshed = harness.refresh_repository(
                        rid, "first", "demo-python312", ["rule.py", "other.py"], approved=True
                    )
                    assert refreshed["snapshot"] == third["snapshot"]
                    assert harness.resume()["status"] == "incomplete"
                    harness.store_artifact("rule.py", approved=True)
                    assert harness.resume()["status"] == "incomplete"
                    harness.store_artifact("other.py", approved=True)
                    packet = harness.resume()
                    assert packet["status"] == "ready"
                    assert (
                        packet["checkpoint"]["state"]["repository"]["snapshot_id"]
                        == third["snapshot"]
                    )
                    return {
                        "status": "passed",
                        "transport": "actual_unix_socket",
                        "snapshots": 3,
                        "paused_worker_unrelated_guidance": paused["status"],
                        "unchanged_comparison": "valid",
                        "changed_dependency": "withheld",
                        "impact": "known_affected",
                        "historical_current": False,
                        "harness_resume": packet["status"],
                        "repository_checks_executed": False,
                        "provider_calls": 0,
                    }
                finally:
                    if harness is not None:
                        harness.close(checkpoint=False)
                    server.shutdown()
                    thread.join()


if __name__ == "__main__":
    print(json.dumps(run(), sort_keys=True))
