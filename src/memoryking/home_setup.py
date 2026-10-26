"""Create a new, disabled home installation. Never enroll accounts or enable mail."""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path
from typing import Any

from memoryking.adapters.local_files import atomic, private_dir, private_file
from memoryking.adapters.local_store import LocalStore
from memoryking.adapters.viewer_config import validate_config
from memoryking.domain.local import LocalError


def configurations(directory: Path) -> None:
    private_dir(directory)
    if any((directory / name).exists() for name in ("viewer.json", "reports.json")):
        raise LocalError("home_configuration_exists")
    viewer: dict[str, Any] = {
        "version": 1,
        "enabled": False,
        "tenant": "main",
        "public_origin": "https://configure-before-use.example.invalid",
        "listen_host": "127.0.0.1",
        "listen_port": 8772,
        "transport": "isolated_proxy",
        "github_client_id": "configure-before-use",
        "github_secret_file": str(directory / "github.secret"),
        "tls_cert_file": None,
        "tls_key_file": None,
        "session_idle_seconds": 1800,
        "session_absolute_seconds": 28800,
        "login_seconds": 300,
        "poll_seconds": 10,
        "display_lifetime_seconds": 15,
        "limits": {
            "active_sessions": 5,
            "pending_logins": 64,
            "list_items": 50,
            "detail_bytes": 16384,
            "graph_nodes": 100,
            "graph_edges": 150,
            "response_bytes": 262144,
            "concurrency": 4,
            "request_seconds": 5,
        },
    }
    validate_config(viewer)
    atomic(directory / "viewer.json", json.dumps(viewer, indent=2).encode())
    atomic(
        directory / "reports.json",
        json.dumps(
            {
                "version": 1,
                "enabled": False,
                "tenant": "main",
                "project": "home",
                "credential_file": None,
                "mail_secret_file": None,
                "sender_enabled": False,
                "poll_seconds": 5,
            },
            indent=2,
        ).encode(),
    )


def postgres_secrets(directory: Path, admin: Path) -> None:
    from psycopg.conninfo import make_conninfo

    private_dir(directory)
    private_dir(admin)
    if any(admin.iterdir()) or (directory / "runtime.dsn").exists():
        raise LocalError("home_database_secrets_exist")
    private_dir(admin / "roles", create=True)
    for label, role in (
        ("bootstrap", "mk_bootstrap"),
        ("migration", "mk_migration"),
        ("runtime", "mk_runtime"),
        ("worker", "mk_worker"),
        ("backup", "mk_backup"),
    ):
        password = secrets.token_urlsafe(32)
        atomic(
            admin / ("bootstrap.password" if label == "bootstrap" else f"roles/{role}"),
            password.encode(),
        )
        target = directory if label == "runtime" else admin
        atomic(
            target / f"{label}.dsn",
            make_conninfo(
                host="/socket",
                user=role,
                dbname="memoryking",
                password=password,
            ).encode(),
        )


def initialize_postgres(data: Path, control: Path, admin: Path) -> None:
    import psycopg
    from psycopg import sql

    from memoryking.adapters.postgres_admin import (
        Roles,
        initialize_tenant,
        migrate,
        provision_roles,
    )
    from memoryking.home import socket_dsn

    if data.exists() or control.exists():
        raise LocalError("initialization_requires_new_directories")
    # Authentication role secrets remain on an operator-only volume.
    passwords = {}
    for role in ("mk_migration", "mk_runtime", "mk_worker", "mk_backup"):
        path = admin / "roles" / role
        private_file(path)
        passwords[role] = path.read_text()
    with psycopg.connect(socket_dsn(admin / "bootstrap.dsn"), autocommit=True) as connection:
        with connection.transaction():
            provision_roles(connection, Roles())
            for role, password in passwords.items():
                connection.execute(
                    sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
    with psycopg.connect(socket_dsn(admin / "migration.dsn"), autocommit=True) as connection:
        migrate(connection)
        initialize_tenant(connection, data, control, "main")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("local", "shared", "postgres-init"))
    parser.add_argument("--data", type=Path, default=Path("/data/store"))
    parser.add_argument("--control", type=Path, default=Path("/control/security"))
    parser.add_argument("--secrets", type=Path, default=Path("/secrets"))
    parser.add_argument("--pgadmin", type=Path, default=Path("/pgadmin"))
    args = parser.parse_args()
    try:
        if args.operation == "postgres-init":
            initialize_postgres(args.data, args.control, args.pgadmin)
        else:
            if args.data.exists() or args.control.exists():
                raise LocalError("initialization_requires_new_directories")
            configurations(args.secrets)
            if args.operation == "local":
                LocalStore.initialize(args.data, args.control)
            else:
                postgres_secrets(args.secrets, args.pgadmin)
        print(
            json.dumps(
                {
                    "prepared": args.operation,
                    "viewer_enabled": False,
                    "reports_enabled": False,
                    "sender_enabled": False,
                }
            )
        )
    except LocalError as error:
        print(json.dumps({"error": error.code}), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"error": "home_setup_failed"}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
