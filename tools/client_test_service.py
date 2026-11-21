#!/usr/bin/env python3
"""Start a real local daemon or shared service for the TypeScript client tests.

`local` needs only Python; `shared` needs a native PostgreSQL (MEMORYKING_PG_BIN). Both
print one JSON line describing the running service, then run until standard input closes.
Every file lives in a private temporary directory that is removed on exit. Nothing here
touches a model provider or leaves the loopback interface.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from memoryking.adapters.local_files import atomic, private_dir
from memoryking.domain.local import LocalError
from memoryking.sdk import LocalClient
from memoryking.shared_sdk import SharedClient


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def environment() -> dict[str, str]:
    skip = {"PYTHONPATH", "PYTHONHOME", "GMAIL_APP_KEY", "GMAIL_FROM", "KINDLE_TO"}
    return {key: value for key, value in os.environ.items() if key not in skip}


def announce(description: dict[str, Any]) -> None:
    print(json.dumps(description), flush=True)
    sys.stdin.read()  # the test closes our input to stop the service


def local(base: Path) -> None:
    store, control, port = base / "s", base / "c", free_port()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "memoryking",
            "init",
            "--store",
            str(store),
            "--control",
            str(control),
        ],
        check=True,
        capture_output=True,
        env=environment(),
    )
    daemon = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "memoryking",
            "serve",
            "--store",
            str(store),
            "--control",
            str(control),
            "--dashboard-port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment(),
    )
    try:
        assert daemon.stdout is not None
        daemon.stdout.readline()  # the ready line
        credential = control / "owner.credential"
        client = LocalClient(store / "service.sock", credential, "demo")
        client.remember("Raising every timeout exceeded the total retry deadline.", source="seed")
        announce(
            {
                "port": port,
                "project": "demo",
                "credential_file": str(credential),
                "incarnation": client.binding[-1],
            }
        )
    finally:
        daemon.terminate()
        daemon.wait(10)


def shared(base: Path) -> None:
    from postgres_dev_cluster import DevCluster

    with DevCluster() as cluster:
        content, control, passwords = base / "content", base / "control", base / "passwords"
        private_dir(passwords, create=True)
        dsns = {}
        for role in ("mk_dev_admin", "mk_migration", "mk_runtime", "mk_worker", "mk_backup"):
            path = base / (role + ".dsn")
            atomic(path, cluster.dsn(role).encode())
            dsns[role] = str(path)
            if role != "mk_dev_admin":
                atomic(passwords / role, secrets.token_urlsafe(32).encode())

        def cli(*arguments: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
            result = subprocess.run(
                [sys.executable, "-m", "memoryking.shared", *arguments],
                input=json.dumps(body) if body else None,
                text=True,
                capture_output=True,
                env=environment(),
                timeout=60,
            )
            if result.returncode:
                raise RuntimeError("Shared CLI failed: " + result.stderr.strip())
            parsed: dict[str, Any] = json.loads(result.stdout)
            return parsed

        cli("provision-roles", "--dsn-file", dsns["mk_dev_admin"], "--role-secrets", str(passwords))
        cli("migrate", "--dsn-file", dsns["mk_migration"])
        common = ["--content", str(content), "--control", str(control)]
        owner = cli("init-tenant", "--dsn-file", dsns["mk_migration"], *common, "--tenant", "demo")[
            "credential_file"
        ]
        port = free_port()
        url = f"http://127.0.0.1:{port}"
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
                "--origin",
                url,
                "--loopback-http",
            ],
            env=environment(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    SharedClient(url, "demo", Path(owner), "demo", loopback_http=True)
                    break
                except LocalError:
                    if server.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Shared service did not start") from None
                    time.sleep(0.05)
            granted: dict[str, str] = {}
            for principal, capabilities in (
                ("reviewer", ["read"]),
                ("reader", ["read"]),
                ("planner", ["read", "capture", "checkpoint", "handoff"]),
            ):
                path = base / (principal + ".credential")
                cli(
                    "request",
                    "--url",
                    url,
                    "--tenant",
                    "demo",
                    "--project",
                    "demo",
                    "--credential",
                    owner,
                    "--credential-output",
                    str(path),
                    "--loopback-http",
                    body={
                        "version": 1,
                        "operation": "grant",
                        "args": {
                            "principal": principal,
                            "project": "demo",
                            "capabilities": capabilities,
                        },
                    },
                )
                granted[principal] = str(path)
            announce(
                {
                    "url": url,
                    "tenant": "demo",
                    "project": "demo",
                    "credential_file": owner,
                    "credentials": granted,
                }
            )
        finally:
            server.terminate()
            server.wait(10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backend", choices=("local", "shared"))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mk-cts-", dir="/tmp") as temporary:
        base = Path(temporary)
        local(base) if args.backend == "local" else shared(base)


if __name__ == "__main__":
    main()
