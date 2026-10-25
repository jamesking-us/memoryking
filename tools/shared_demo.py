#!/usr/bin/env python3
"""Exercise real shared CLI, native PostgreSQL, HTTP and worker/backup role separation."""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from postgres_dev_cluster import DevCluster
from shared_dev_tls import create as create_test_tls

from memoryking.adapters.local_files import atomic, private_dir
from memoryking.domain.local import LocalError
from memoryking.shared_sdk import SharedClient


def main() -> None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "GMAIL_APP_KEY", "GMAIL_FROM", "KINDLE_TO"}
    }
    with (
        DevCluster() as cluster,
        tempfile.TemporaryDirectory(prefix="mk-shared-demo-", dir="/tmp") as temporary,
    ):
        base = Path(temporary)
        content, control, passwords = base / "content", base / "control", base / "passwords"
        private_dir(passwords, create=True)
        dsns = {}
        for role in ("mk_dev_admin", "mk_migration", "mk_runtime", "mk_worker", "mk_backup"):
            path = base / (role + ".dsn")
            atomic(path, cluster.dsn(role).encode())
            dsns[role] = str(path)
            if role != "mk_dev_admin":
                atomic(passwords / role, secrets.token_urlsafe(32).encode())

        def cli(*arguments: str, body: dict | None = None) -> dict:
            result = subprocess.run(
                [sys.executable, "-m", "memoryking.shared", *arguments],
                input=json.dumps(body) if body else None,
                text=True,
                capture_output=True,
                env=environment,
                timeout=30,
            )
            if result.returncode:
                raise RuntimeError("Shared CLI failed: " + result.stderr.strip())
            return json.loads(result.stdout)

        cli("provision-roles", "--dsn-file", dsns["mk_dev_admin"], "--role-secrets", str(passwords))
        cli("migrate", "--dsn-file", dsns["mk_migration"])
        common = ["--content", str(content), "--control", str(control)]
        owner = cli("init-tenant", "--dsn-file", dsns["mk_migration"], *common, "--tenant", "demo")[
            "credential_file"
        ]
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        url = f"https://127.0.0.1:{port}"
        cert, key = create_test_tls(base)
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "memoryking.shared",
                "serve",
                "--dsn-file",
                dsns["mk_runtime"],
                *common,
                "--port",
                str(port),
                "--host",
                f"127.0.0.1:{port}",
                "--tls-cert",
                str(cert),
                "--tls-key",
                str(key),
                "--origin",
                url,
                "--plan-root",
                str(Path(__file__).resolve().parents[1]),
                "--dashboard-tenant",
                "demo",
            ],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    SharedClient(url, "demo", Path(owner), "project", ca_file=cert)
                    break
                except LocalError:
                    if server.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Shared CLI did not start") from None
                    time.sleep(0.05)
            # The service certificate is not globally trusted and the client must not bypass TLS.
            try:
                SharedClient(url, "demo", Path(owner), "project")
            except LocalError:
                pass
            else:
                raise RuntimeError("Untrusted TLS certificate was accepted")
            bootstrap = base / "bootstrap.json"
            cli(
                "request",
                "--url",
                url,
                "--tenant",
                "demo",
                "--project",
                "project",
                "--credential",
                owner,
                "--ca-file",
                str(cert),
                "--bootstrap-output",
                str(bootstrap),
                body={"version": 1, "operation": "dashboard_bootstrap", "args": {}},
            )
            assert json.loads(bootstrap.read_text())["url"] == url
            assert bootstrap.stat().st_mode & 0o077 == 0
            principal_files = {}
            for principal, capabilities in (
                ("planner", ["read", "capture", "checkpoint", "handoff"]),
                ("implementer", ["read", "capture", "checkpoint", "repository"]),
                ("reviewer", ["read", "review", "attest"]),
                ("worker", ["read", "jobs"]),
            ):
                path = base / (principal + ".credential")
                cli(
                    "request",
                    "--url",
                    url,
                    "--tenant",
                    "demo",
                    "--project",
                    "project",
                    "--credential",
                    owner,
                    "--credential-output",
                    str(path),
                    "--ca-file",
                    str(cert),
                    body={
                        "version": 1,
                        "operation": "grant",
                        "args": {
                            "principal": principal,
                            "project": "project",
                            "capabilities": capabilities,
                        },
                    },
                )
                principal_files[principal] = path
                subject = SharedClient(url, "demo", path, "project", ca_file=cert)
                assert subject.context()["principal"] == principal
            planner = SharedClient(url, "demo", principal_files["planner"], "project", ca_file=cert)
            first = planner.capture(
                "observation",
                {
                    "version": 1,
                    "project": "project",
                    "source": "shared-demo",
                    "source_event": "one",
                    "text": "Reviewed shared role rehearsal",
                    "readers": None,
                    "classification": "internal",
                    "observed_at_ms": 0,
                },
            )
            worker = cli(
                "worker-once",
                "--dsn-file",
                dsns["mk_worker"],
                *common,
                "--tenant",
                "demo",
                "--project",
                "project",
                "--credential",
                str(principal_files["worker"]),
            )
            assert worker["status"] == "complete"
            archive = base / "archive"
            cli(
                "backup",
                "--dsn-file",
                dsns["mk_runtime"],
                "--backup-dsn-file",
                dsns["mk_backup"],
                *common,
                "--tenant",
                "demo",
                "--credential",
                owner,
                "--archive",
                str(archive),
            )
            restored = cli(
                "restore",
                "--dsn-file",
                dsns["mk_migration"],
                *common,
                "--tenant",
                "demo",
                "--archive",
                str(archive),
            )
            assert restored["reconciliation_required"]
            recovered = SharedClient(url, "demo", Path(owner), "project", ca_file=cert)
            assert recovered.binding[-1] != first["incarnation"]
            try:
                planner.context()
            except LocalError as exc:
                assert exc.code == "history_changed"
            else:
                raise RuntimeError("Old shared credential survived restore")
            print(
                json.dumps(
                    {
                        "status": "passed",
                        "backend": "PostgreSQL",
                        "roles": ["planner", "implementer", "reviewer"],
                        "worker_role": "mk_worker",
                        "backup_role": "mk_backup",
                        "old_history_rejected": True,
                        "provider_calls": 0,
                        "network": "verified loopback HTTPS; private Unix database socket",
                        "untrusted_tls_rejected": True,
                        "private_dashboard_bootstrap": True,
                    }
                )
            )
        finally:
            server.terminate()
            try:
                server.wait(10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(5)


if __name__ == "__main__":
    main()
