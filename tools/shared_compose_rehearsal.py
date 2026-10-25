#!/usr/bin/env python3
"""Run the disposable, explicit local container rehearsal; preserve state and evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from memoryking.adapters.local_files import atomic
from memoryking.domain.local import LocalError
from memoryking.shared_sdk import SharedClient

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True, help="New private state directory")
    parser.add_argument(
        "--output", type=Path, required=True, help="New sanitized evidence directory"
    )
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
    state, output = args.directory.resolve(), args.output.resolve()
    if state.exists() or output.exists():
        raise SystemExit("Use new state and output directories; previous attempts are retained")
    output.mkdir(parents=True)
    environment = {
        k: v
        for k, v in os.environ.items()
        if k not in {"GMAIL_FROM", "GMAIL_APP_KEY", "KINDLE_TO", "PYTHONHOME", "PYTHONPATH"}
    }
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "source_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "scope": "Local single-host Linux container rehearsal, not production or a full Linux test suite",
        "steps": [],
        "passed": False,
        "provider_calls": 0,
    }
    compose = [
        str(args.compose.resolve()),
        "--env-file",
        str(state / "compose.env"),
        "-f",
        str(ROOT / "deploy/shared/compose.yaml"),
    ]

    def run(
        label: str, command: list[str], timeout: int = 120, expected: tuple[int, ...] = (0,)
    ) -> str:
        started = time.monotonic()
        result = subprocess.run(
            command, cwd=ROOT, env=environment, capture_output=True, text=True, timeout=timeout
        )
        (output / (label + ".log")).write_text(result.stdout + result.stderr)
        report["steps"].append(
            {
                "name": label,
                "exit_code": result.returncode,
                "seconds": round(time.monotonic() - started, 3),
                "log": label + ".log",
                "expected_exit_codes": list(expected),
            }
        )
        print(("PASS " if result.returncode in expected else "FAIL ") + label, flush=True)
        if result.returncode not in expected:
            raise RuntimeError(label + " failed; see sanitized command log")
        return result.stdout

    def operator(label: str, *command: str) -> dict:
        return json.loads(run(label, [*compose, "run", "--rm", "--no-deps", "operator", *command]))

    started_containers = False
    try:
        run("engine", ["docker", "version", "--format", "{{json .Server}}"])
        run("compose", [str(args.compose.resolve()), "version"])
        run(
            "prepare",
            [
                sys.executable,
                "tools/shared_compose_prepare.py",
                "--directory",
                str(state),
                "--port",
                str(args.port),
            ],
        )
        run("configuration", [*compose, "config", "--format", "json"])
        base_image = re.search(
            r"^FROM (\S+)$", (ROOT / "deploy/shared/Dockerfile").read_text(), re.MULTILINE
        ).group(1)
        run(
            "thread-preflight",
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                base_image,
                "python",
                "-c",
                "import threading; t=threading.Thread(target=lambda: print('thread-ready')); t.start(); t.join()",
            ],
        )
        # Use the engine's build command so this rehearsal does not require a
        # separately installed buildx plugin merely to build the one local image.
        run(
            "build",
            [
                "docker",
                "build",
                "--force-rm",
                "--file",
                "deploy/shared/Dockerfile",
                "--tag",
                "memoryking-shared:local",
                ".",
            ],
            900,
        )
        started_containers = True
        run(
            "postgres-up", [*compose, "up", "-d", "--wait", "--wait-timeout", "90", "postgres"], 180
        )
        operator(
            "roles",
            "provision-roles",
            "--dsn-file",
            "/secrets/bootstrap.dsn",
            "--role-secrets",
            "/secrets/roles",
        )
        operator("migrate", "migrate", "--dsn-file", "/secrets/migration.dsn")
        common = [
            "--content",
            "/content-volume/data",
            "--control",
            "/control-volume/data",
            "--tenant",
            "demo",
        ]
        operator("tenant", "init-tenant", "--dsn-file", "/secrets/migration.dsn", *common)
        run("api-up", [*compose, "up", "-d", "gateway"])
        gateway_id = run("gateway-id", [*compose, "ps", "-q", "gateway"]).strip()
        ports = json.loads(
            run(
                "gateway-port",
                ["docker", "inspect", gateway_id, "--format", "{{json .NetworkSettings.Ports}}"],
            )
        )
        assert ports["8765/tcp"] == [{"HostIp": "127.0.0.1", "HostPort": str(args.port)}]
        run(
            "api-network-isolation",
            [
                *compose,
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                "from pathlib import Path; rows=Path('/proc/net/route').read_text().splitlines()[1:]; assert not any(row.split()[1]=='00000000' for row in rows); print('No IPv4 default route in API container')",
            ],
        )
        url, cert = f"https://127.0.0.1:{args.port}", state / "secrets/api/test-cert.pem"
        owner_file = state / "control/data/demo/owner.credential"
        deadline = time.monotonic() + 30
        while True:
            try:
                owner = SharedClient(url, "demo", owner_file, "demo", ca_file=cert)
                break
            except LocalError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Container HTTPS did not become ready") from None
                time.sleep(0.2)
        try:
            SharedClient(url, "demo", owner_file, "demo")
        except LocalError:
            pass
        else:
            raise RuntimeError("Untrusted certificate accepted")
        clients = {}
        for principal, capabilities in (
            ("planner", ["read", "capture", "checkpoint", "handoff"]),
            ("implementer", ["read", "capture", "checkpoint", "repository"]),
            ("reviewer", ["read", "review", "attest"]),
            ("worker", ["read", "jobs"]),
        ):
            issued = owner._request(
                owner._credential(),
                "grant",
                {"principal": principal, "project": "demo", "capabilities": capabilities},
            )
            credential_directory = state / "secrets"
            if principal == "worker":
                credential_directory /= "worker"
            path = credential_directory / (principal + ".credential")
            atomic(path, issued["credential"].encode())
            clients[principal] = SharedClient(url, "demo", path, "demo", ca_file=cert)
            assert clients[principal].context()["principal"] == principal
        observation = {
            "version": 1,
            "project": "demo",
            "source": "compose-rehearsal",
            "source_event": "one",
            "text": "Synthetic container role rehearsal",
            "readers": None,
            "classification": "internal",
            "observed_at_ms": 0,
        }
        first = clients["planner"].capture("first", observation)
        # Prove actual Docker supervision and acknowledged-data survival; an
        # on-failure policy in YAML alone is not executed recovery evidence.
        for component in ("api", "postgres"):
            component_id = run(component + "-id", [*compose, "ps", "-q", component]).strip()
            run(
                component + "-crash",
                [
                    "docker",
                    "exec",
                    component_id,
                    "/bin/sh",
                    "-c",
                    'read children < /proc/1/task/1/children; for child in $children; do kill -KILL "$child"; done',
                ],
                expected=(0, 137),
            )
            deadline = time.monotonic() + 45
            while True:
                try:
                    state = subprocess.run(
                        [
                            "docker",
                            "inspect",
                            component_id,
                            "--format",
                            "{{.RestartCount}} {{.State.Running}}",
                        ],
                        capture_output=True,
                        text=True,
                        env=environment,
                        timeout=10,
                    )
                    if state.returncode or not re.fullmatch(r"[1-9][0-9]* true\s*", state.stdout):
                        raise LocalError("supervisor_not_restarted")
                    recovered = clients["planner"]._request(
                        clients["planner"]._credential(),
                        "read",
                        {"project": "demo", "observation": first["handle"]},
                    )
                    assert recovered["items"][0]["id"] == first["observation"]
                    break
                except LocalError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            component + " did not recover acknowledged capture"
                        ) from None
                    time.sleep(0.2)
            restarts = int(
                run(
                    component + "-restart-count",
                    ["docker", "inspect", component_id, "--format", "{{.RestartCount}} "],
                )
            )
            assert restarts >= 1
        try:
            clients["reviewer"].capture("forbidden", {**observation, "source_event": "forbidden"})
        except LocalError as exc:
            assert exc.code == "denied"
        else:
            raise RuntimeError("Reviewer gained capture authority")
        worker = json.loads(run("worker", [*compose, "run", "--rm", "--no-deps", "worker"]))
        assert worker["status"] == "complete"
        operator(
            "backup",
            "backup",
            "--dsn-file",
            "/secrets/api/runtime.dsn",
            "--backup-dsn-file",
            "/secrets/backup.dsn",
            *common,
            "--credential",
            "/control-volume/data/demo/owner.credential",
            "--archive",
            "/archives/first",
        )
        clients["planner"].capture("lost", {**observation, "source_event": "lost"})
        owner._request(owner._credential(), "revoke", {"principal": "reviewer", "project": "demo"})
        result = operator(
            "restore",
            "restore",
            "--dsn-file",
            "/secrets/migration.dsn",
            *common,
            "--archive",
            "/archives/first",
        )
        assert result["reconciliation_required"]
        new_owner = SharedClient(url, "demo", owner_file, "demo", ca_file=cert)
        assert new_owner.context()["incarnation"] != first["incarnation"]
        try:
            clients["planner"].context()
        except LocalError as exc:
            assert exc.code == "history_changed"
        else:
            raise RuntimeError("Old container history survived restore")
        run(
            "migration-cli",
            [
                sys.executable,
                "tools/migration_demo.py",
                "--shared-url",
                url,
                "--tenant",
                "demo",
                "--project",
                "migration-demo",
                "--credential",
                str(owner_file),
                "--ca-file",
                str(cert),
            ],
        )
        run(
            "runtime",
            [
                *compose,
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                "import platform; print(platform.platform()); print(platform.python_version())",
            ],
        )
        run("images", [*compose, "images", "--format", "json"])
        report.update(
            passed=True,
            distinct_principals=True,
            reviewer_capture_denied=True,
            verified_loopback_https=True,
            untrusted_tls_rejected=True,
            worker_role="mk_worker",
            backup_role="mk_backup",
            restored_history_retired=True,
            supervised_api_and_database_crash_recovery=True,
            legacy_migration_cli_verified=True,
            state_retained=True,
        )
    except Exception as exc:
        report["failure"] = {"type": type(exc).__name__, "summary": str(exc)}
        raise
    finally:
        if started_containers:
            # Preserve startup/runtime diagnostics before removing these fixture
            # services. The API/PostgreSQL logs do not include bearer/DSN values.
            try:
                run("service-logs", [*compose, "logs", "--no-color", "api", "postgres", "gateway"])
            except Exception:
                report["service_logs_failed"] = True
            try:
                run("shutdown", [*compose, "down", "--timeout", "10"])
            except Exception:
                report["passed"] = False
                report["shutdown_failed"] = True
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["input_hashes"] = {
            p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
            for p in (
                "deploy/shared/compose.yaml",
                "deploy/shared/Dockerfile",
                "tools/shared_compose_rehearsal.py",
            )
        }
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
