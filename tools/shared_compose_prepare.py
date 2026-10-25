#!/usr/bin/env python3
"""Prepare a new, private, disposable Compose rehearsal; never starts/deploys it."""

from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path

from psycopg.conninfo import make_conninfo
from shared_dev_tls import create

from memoryking.adapters.local_files import atomic, private_dir


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    base = args.directory.resolve()
    if base.exists() or not 1024 <= args.port <= 65535 or any(c in str(base) for c in '\n\r"$'):
        raise SystemExit("Use a new directory, a port 1024..65535 and a simple path")
    private_dir(base, create=True)
    for name in ("content", "control", "postgres", "secrets", "archives"):
        private_dir(base / name, create=True)
    for name in ("roles", "api", "worker", "postgres"):
        private_dir(base / "secrets" / name, create=True)
    for name in ("content", "control"):
        private_dir(base / name / "data", create=True)
    for label, role in [
        ("bootstrap", "mk_bootstrap"),
        ("migration", "mk_migration"),
        ("runtime", "mk_runtime"),
        ("worker", "mk_worker"),
        ("backup", "mk_backup"),
    ]:
        password = secrets.token_urlsafe(32)
        if label == "bootstrap":
            atomic(base / "secrets" / "postgres" / "bootstrap.password", password.encode())
        else:
            atomic(base / "secrets" / "roles" / role, password.encode())
        secret_directory = base / "secrets"
        if label in {"runtime", "worker"}:
            secret_directory /= "api" if label == "runtime" else "worker"
        atomic(
            secret_directory / (label + ".dsn"),
            make_conninfo(
                host="/socket", user=role, dbname="memoryking", password=password
            ).encode(),
        )
    create(base / "secrets" / "api")
    atomic(
        base / "compose.env",
        (
            f'MK_STATE="{base}"\nMK_UID={os.getuid()}\nMK_GID={os.getgid()}\nMK_PORT={args.port}\n'
        ).encode(),
    )
    print(
        "Prepared private fixture at "
        + str(base)
        + "; no container started. Test certificate expires in one day."
    )


if __name__ == "__main__":
    main()
