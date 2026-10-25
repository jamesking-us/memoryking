"""Disposable real CLI/socket P03 walkthrough; no model, SMTP or production data."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from memoryking.adapters.local_ipc import request


def body(span, text):
    return {
        "text": text,
        "kind": "knowledge",
        "selector": {"selector_version": "1", "any_of": [{"all_of": [{"op": "any"}]}]},
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
    }


def main():
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"PYTHONPATH", "PYTHONHOME", "GMAIL_FROM", "GMAIL_APP_KEY", "KINDLE_TO"}
    }
    with tempfile.TemporaryDirectory(prefix="mk-knowledge-", dir="/tmp") as directory:
        root = Path(directory)
        data = root / "db"
        control = root / "ctl"
        base = [sys.executable, "-m", "memoryking"]
        subprocess.run(
            base + ["init", "--store", str(data), "--control", str(control)],
            env=env,
            check=True,
            capture_output=True,
        )
        process = subprocess.Popen(
            base + ["serve", "--store", str(data), "--control", str(control)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            ready = json.loads(process.stdout.readline())
            assert ready["ready"]
            socket = data / "service.sock"
            credential = (control / "owner.credential").read_text()
            namespace = request(socket, credential, "namespace", {"project": "demo"})["namespace"]
            raw = "Run observed timeout=30 and retry recovery."
            obs = request(
                socket,
                credential,
                "capture",
                {
                    "namespace": namespace,
                    "key": "observation",
                    "request": {
                        "version": 1,
                        "project": "demo",
                        "source": "demo",
                        "source_event": "e1",
                        "text": raw,
                        "readers": None,
                        "classification": "internal",
                        "observed_at_ms": 1,
                    },
                },
            )
            span = {
                "observation": obs["observation"],
                "start_byte": 0,
                "end_byte": len(raw.encode()),
                "content_hash": hashlib.sha256(raw.encode()).hexdigest(),
            }
            history = obs["incarnation"]

            def invoke(action, args, key=None):
                command = {
                    "version": 1,
                    "incarnation": history,
                    "action": action,
                    "args": {"project": "demo", **args},
                }
                if key:
                    command["key"] = key
                return request(socket, credential, "knowledge", {"command": command})

            proposed = invoke(
                "propose",
                {
                    "logical_key": "timeout",
                    "expected_version": 0,
                    "body": body(span, "Use timeout=30 under the reviewed conditions."),
                },
                "proposal",
            )
            assert invoke("query", {"bindings": [{}]})["items"] == []
            accepted = invoke(
                "review",
                {
                    "revision": proposed["revision"],
                    "expected_version": 1,
                    "decision": "accept",
                    "reason": "operator_review",
                    "breadth_reviewed": True,
                },
                "accept",
            )
            result = invoke("query", {"bindings": [{}]})
            assert result["items"][0]["revision"] == accepted["revision"]
            invoke(
                "review",
                {
                    "revision": proposed["revision"],
                    "expected_version": 2,
                    "decision": "quarantine",
                    "reason": "needs_investigation",
                    "breadth_reviewed": False,
                },
                "quarantine",
            )
            assert invoke("query", {"bindings": [{}]})["status"] == "blocked"
            assert invoke("query", {"bindings": [{}], "diagnostic": True})["items"][0][
                "quarantined"
            ]
            assert invoke("constraints", {"bindings": [{}]})["status"] == "complete"
            episode = request(socket, credential, "read", {"project": "demo", "query": "timeout"})[
                "items"
            ][0]
            assert episode["authority"] == "observed_episode"
            print(
                json.dumps(
                    {
                        "passed": True,
                        "checks": [
                            "propose_without_promotion",
                            "versioned_acceptance",
                            "quarantine_blocks",
                            "diagnostic_review",
                            "complete_empty_policy",
                            "attributed_no_model_episode",
                        ],
                        "transport": "actual CLI daemon and Unix socket",
                    }
                )
            )
        finally:
            process.terminate()
            process.wait(timeout=10)


if __name__ == "__main__":
    main()
