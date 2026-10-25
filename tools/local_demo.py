#!/usr/bin/env python3
"""Exercise documented CLI, socket, capture, workers, policy and offline recovery privately."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ENV = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}


def run(*args, body=None, expected=0):
    proc = subprocess.run(
        [sys.executable, "-m", "memoryking", *map(str, args)],
        input=json.dumps(body) if body else None,
        text=True,
        capture_output=True,
        env=ENV,
        timeout=15,
    )
    if proc.returncode != expected:
        raise RuntimeError(f"CLI exit {proc.returncode}: {proc.stderr}")
    return json.loads(proc.stdout if expected == 0 else proc.stderr)


def main():
    checks = []
    with tempfile.TemporaryDirectory(prefix="mk-demo-", dir="/tmp") as name:
        base = Path(name)
        root, control, backup = base / "db", base / "control", base / "backup"
        common = ["--store", root, "--control", control]
        credential = Path(run("init", *common)["credential_file"])
        checks.append("private_initialization")

        def rpc(operation, args, who=credential, expected=0, extra=()):
            return run(
                "request",
                "--socket",
                root / "service.sock",
                "--credential",
                who,
                *extra,
                body={"operation": operation, "args": args},
                expected=expected,
            )

        def start():
            server = subprocess.Popen(
                [sys.executable, "-m", "memoryking", "serve", *map(str, common)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=ENV,
            )
            if not json.loads(server.stdout.readline())["ready"]:
                raise RuntimeError("server failed")
            return server

        server = start()
        try:
            namespace = rpc("namespace", {"project": "demo"})["namespace"]
            capture = {
                "namespace": namespace,
                "key": "demo-request",
                "request": {
                    "version": 1,
                    "project": "demo",
                    "source": "cli-demo",
                    "source_event": "event-1",
                    "text": "MemoryKing captures this synthetic observation.",
                    "readers": None,
                    "classification": "internal",
                    "observed_at_ms": 0,
                },
            }
            item = rpc("capture", capture)
            assert rpc("capture", capture) == item
            checks.append("idempotent_capture")
            fetched = rpc("read", {"project": "demo", "observation": item["handle"]})
            assert fetched["items"][0]["text"] == capture["request"]["text"]
            checks.append("authorized_receipt_and_evidence")
            lease = rpc("claim", {"project": "demo"})["job"]
            rpc("finish", {"project": "demo", "lease": lease["lease"], "outcome": "complete"})
            assert rpc("status", {"project": "demo", "watermark": item["watermark"]})["satisfied"]
            checks.append("fenced_job_and_contiguous_frontier")
            delegated = control / "reader.credential"
            rpc(
                "grant",
                {"principal": "reader", "project": "demo", "capabilities": ["read", "export"]},
                extra=("--credential-output", delegated),
            )
            assert rpc("export", {"project": "demo"}, who=delegated)["items"]
            rpc("revoke", {"principal": "reader", "project": "demo"})
            assert rpc("read", {"project": "demo"}, who=delegated, expected=2)["error"] == "denied"
            checks.append("delegation_export_and_revocation")
        finally:
            server.terminate()
            server.communicate(timeout=10)
        run("backup", *common, "--backup", backup)
        checks.append("offline_consistent_backup")
        server = start()
        try:
            rpc("purge", {"observation": item["observation"]})
        finally:
            server.terminate()
            server.communicate(timeout=10)
        result = run("restore", *common, "--backup", backup)
        assert result["reconciliation_required"]
        server = start()
        try:
            assert not rpc("read", {"project": "demo"})["items"]
            assert (
                rpc("status", {"project": "demo", "watermark": item["watermark"]}, expected=2)[
                    "error"
                ]
                == "history_changed"
            )
            assert rpc("capture", capture, expected=2)["error"] == "history_changed"
            checks.append("restore_replays_purge_and_retires_history")
        finally:
            server.terminate()
            server.communicate(timeout=10)
        run("gc", *common)
        checks.append("offline_garbage_collection")
    print(
        json.dumps(
            {
                "status": "passed",
                "checks": checks,
                "data": "synthetic_disposable",
                "delivery": "no_email_or_model_calls",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
