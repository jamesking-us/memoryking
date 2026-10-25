#!/usr/bin/env python3
"""Disposable synthetic UI workspace. No model, SMTP, or production data access."""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from memoryking.adapters.dashboard import Dashboard
from memoryking.adapters.handoff import Handoff
from memoryking.adapters.knowledge_store import Knowledge
from memoryking.adapters.local_files import atomic
from memoryking.adapters.local_store import LocalStore
from memoryking.adapters.repository_store import Repository
from memoryking.adapters.retrieval import Retrieval
from memoryking.adapters.working import Working

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=24, choices=range(1, 241), metavar="1..240")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mk-dashboard-demo-") as directory:
        base = Path(directory)
        credential = LocalStore.initialize(base / "data", base / "control").read_text()
        with LocalStore(base / "data", base / "control") as store:
            ns = store.namespace(credential, "demo")
            k, w = Knowledge(store), Working(store)

            def command(service, action, data, key=None):
                output = []
                service.execute(
                    credential,
                    {
                        "version": 1,
                        "incarnation": store.incarnation,
                        "action": action,
                        "args": data,
                        **({"key": key} if key else {}),
                    },
                ).emit(output.append)
                return json.loads(output[0])

            observations = []
            for i in range(args.count):
                text = f"DEMO component {i:03d}: timeout observed; cause remains unverified."
                obs = store.capture(
                    credential,
                    ns,
                    f"obs{i}",
                    {
                        "version": 1,
                        "project": "demo",
                        "source": "synthetic-dashboard-fixture",
                        "source_event": f"obs{i}",
                        "text": text,
                        "readers": None,
                        "classification": "internal",
                        "observed_at_ms": store.clock.utc_ms(),
                    },
                )
                observations.append(obs["observation"])
                body = {
                    "text": f"DEMO guidance {i:03d}: inspect the evidence before retrying.",
                    "kind": "knowledge",
                    "selector": {"selector_version": "1", "any_of": [{"all_of": [{"op": "any"}]}]},
                    "valid_start": None,
                    "valid_end": None,
                    "task": None,
                    "run": None,
                    "authority": None,
                    "rule_key": None,
                    "effect": None,
                    "rule_value": None,
                    "provenance": {},
                    "readers": None,
                    "evidence": [
                        {
                            "observation": obs["observation"],
                            "start_byte": 0,
                            "end_byte": len(text.encode()),
                            "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                        }
                    ],
                }
                if i == 0:
                    body["text"] = (
                        '<img src="https://example.invalid/x" onerror="alert(1)"> DEMO source text; never execute.'
                    )
                claim = command(
                    k,
                    "propose",
                    {
                        "project": "demo",
                        "logical_key": f"guide{i}",
                        "expected_version": 0,
                        "body": body,
                    },
                    f"propose{i}",
                )
                if i:
                    command(
                        k,
                        "review",
                        {
                            "project": "demo",
                            "revision": claim["revision"],
                            "expected_version": claim["family_version"],
                            "decision": "accept",
                            "reason": "synthetic_fixture",
                            "breadth_reviewed": True,
                        },
                        f"review{i}",
                    )
            checkpoint = json.loads((ROOT / "fixtures/contracts/checkpoint.json").read_text())
            checkpoint["objective"] = "DEMO · Inspect recoverable delivery with evidence"
            checkpoint["current_state"] = "Synthetic checkpoint; never real-task qualification"
            checkpoint["acceptance"] = [
                {
                    "id": "evidence",
                    "text": "Inspect current evidence",
                    "state": "passed",
                    "evidence": [{"kind": "observation", "id": observations[0]}],
                    "decision": None,
                },
                {
                    "id": "review",
                    "text": "Independent human review",
                    "state": "pending",
                    "evidence": [],
                    "decision": None,
                },
            ]
            scope = {"project": "demo", "task": "demo-delivery", "run": None}
            for revision in range(3):
                if revision == 1:
                    checkpoint["acceptance"][0]["state"] = "pending"
                if revision == 2:
                    checkpoint["acceptance"].append(
                        {
                            "id": "new_scope",
                            "text": "DEMO scope added after baseline",
                            "state": "pending",
                            "evidence": [],
                            "decision": None,
                        }
                    )
                saved = command(
                    w,
                    "save",
                    {
                        **scope,
                        "expected_revision": revision,
                        "readers": ["owner", "reviewer"],
                        "checkpoint": checkpoint,
                    },
                    f"checkpoint{revision}",
                )
            if args.count == 1:
                command(
                    k,
                    "review",
                    {
                        "project": "demo",
                        "revision": claim["revision"],
                        "expected_version": claim["family_version"],
                        "decision": "accept",
                        "reason": "synthetic_fixture",
                        "breadth_reviewed": True,
                    },
                    "single-review",
                )
            store.grant(credential, "reviewer", "demo", ["read"])
            command(
                Handoff(store),
                "prepare",
                {
                    **scope,
                    "checkpoint": saved["checkpoint"],
                    "recipient": "reviewer",
                    "role": "review",
                    "stage": "verification",
                    "bindings": [{}],
                    "ttl_ms": 3600000,
                },
                "demo-handoff",
            )
            code = base / "code"
            code.mkdir()

            def git(*values):
                subprocess.run(["git", "-C", str(code), *values], check=True, capture_output=True)

            git("init", "-q")
            git("config", "user.name", "Synthetic fixture")
            git("config", "user.email", "fixture@example.invalid")
            (code / "rule.py").write_text("timeout = 30\n")
            (code / "notes.txt").write_text("Unsupported parser coverage fixture\n")
            git("add", ".")
            git("commit", "-qm", "Disposable dashboard fixture")
            repo = Repository(store)
            rid = command(
                repo,
                "register",
                {
                    "project": "demo",
                    "remote": "local:dashboard-demo",
                    "clone": "first",
                    "path": str(code),
                    "readers": None,
                },
                "register",
            )["repository"]
            first = command(
                repo,
                "capture",
                {"project": "demo", "repository": rid, "clone": "first", "environment": "demo"},
                "capture1",
            )
            command(
                repo,
                "bind",
                {
                    "project": "demo",
                    "revision": claim["revision"],
                    "repository": rid,
                    "snapshot": first["snapshot"],
                    "dependencies": [{"kind": "file", "target": "rule.py", "policy": "invalidate"}],
                    "impact": "high",
                    "coverage_reviewed": True,
                    "expected_policy": 0,
                    "evidence": observations[-1],
                },
                "bind",
            )
            (code / "rule.py").write_text("timeout = 90\n")
            last = command(
                repo,
                "capture",
                {"project": "demo", "repository": rid, "clone": "first", "environment": "demo"},
                "capture2",
            )
            command(
                Retrieval(store),
                "brief",
                {
                    "project": "demo",
                    "query": "timeout",
                    "bindings": [{}],
                    "max_output_tokens": 6000,
                    "tokenizer": "cl100k_base",
                },
            )
            web = Dashboard(store, ROOT)
            web.start()

            def bootstrap():
                issued = web.issue(credential)
                path = base / "control" / "browser-login.json"
                atomic(path, json.dumps(issued).encode())
                return {"url": issued["url"], "bootstrap_file": str(path)}

            print(
                json.dumps(
                    {
                        "synthetic": True,
                        "project": "demo",
                        "observations": len(observations),
                        "repository": rid,
                        "snapshot": last["snapshot"],
                        "paths": ["rule.py", "notes.txt"],
                        "retention_preview_observation": observations[0],
                        **bootstrap(),
                    }
                ),
                flush=True,
            )
            try:
                for line in sys.stdin:
                    action = line.strip()
                    if action == "stop":
                        break
                    if action == "bootstrap":
                        print(json.dumps(bootstrap()), flush=True)
                    elif action == "revoke":
                        store.grant(credential, "fixture-reader", "demo", ["read"])
                        store.revoke(credential, "fixture-reader", "demo")
                        print('{"sessions_retired_by_policy_epoch":true}', flush=True)
            finally:
                web.stop()


if __name__ == "__main__":
    main()
