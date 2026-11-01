"""MemoryKing shared-profile operator commands. Secrets are private files, never stdout."""

from __future__ import annotations

import argparse
import ipaddress
import sys
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from memoryking.adapters.local_files import atomic, private_dir, private_file
from memoryking.adapters.postgres_admin import (
    Roles,
    enable_vector,
    initialize_tenant,
    migrate,
    provision_roles,
)
from memoryking.adapters.postgres_pool import create_pool
from memoryking.adapters.postgres_recovery import backup, end_maintenance, restore
from memoryking.adapters.postgres_store import PostgresStore
from memoryking.adapters.shared_http import create_app
from memoryking.adapters.shared_service import SharedService
from memoryking.contracts.canonical import canonical_bytes, load_strict
from memoryking.domain.local import LocalError
from memoryking.observability import LEVELS, configure
from memoryking.shared_sdk import SharedClient


def secret(path: Path) -> str:
    private_file(path)
    with path.open("rb") as stream:
        raw = stream.read(8193)
    if not raw.strip() or len(raw) > 8192:
        raise LocalError("invalid_secret_file")
    return raw.decode().strip()


def dsn(path: Path) -> str:
    value = secret(path)
    options = psycopg.conninfo.conninfo_to_dict(value)
    host = str(options.get("host") or "")
    if not host.startswith("/") and options.get("sslmode") != "verify-full":
        raise LocalError("database_tls_required")
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for name in (
        "provision-roles",
        "migrate",
        "enable-vector",
        "init-tenant",
        "serve",
        "worker-once",
        "backup",
        "restore",
        "end-maintenance",
    ):
        item = commands.add_parser(name)
        item.add_argument("--dsn-file", type=Path, required=True)
        if name == "provision-roles":
            item.add_argument(
                "--role-secrets",
                type=Path,
                required=True,
                help="Private directory with mk_migration, mk_runtime, mk_worker, mk_backup password files",
            )
        if name in {"init-tenant", "serve", "worker-once", "backup", "restore"}:
            item.add_argument("--content", type=Path, required=True)
            item.add_argument("--control", type=Path, required=True)
            item.add_argument("--audience", default="memoryking-shared")
        if name in {"init-tenant", "worker-once", "backup", "restore", "end-maintenance"}:
            item.add_argument("--tenant", required=True)
        if name == "end-maintenance":
            item.add_argument("--control", type=Path, required=True)
            item.add_argument("--audience", default="memoryking-shared")
            item.add_argument("--credential", type=Path, required=True)
            item.add_argument(
                "--operation", required=True, help="Operation id recorded by the dead backup"
            )
        if name == "init-tenant":
            item.add_argument("--principal", default="owner")
        if name in {"worker-once", "backup"}:
            item.add_argument("--credential", type=Path, required=True)
        if name == "worker-once":
            item.add_argument("--project", required=True)
        if name == "backup":
            item.add_argument("--backup-dsn-file", type=Path, required=True)
        if name in {"backup", "restore"}:
            item.add_argument("--archive", type=Path, required=True)
        if name == "restore":
            item.add_argument("--lost-anchor-journal-head")
        if name == "serve":
            item.add_argument("--bind", default="127.0.0.1")
            item.add_argument("--port", type=int, default=8765)
            item.add_argument("--host", action="append", required=True)
            item.add_argument("--origin", action="append", default=[])
            item.add_argument("--tls-cert", type=Path)
            item.add_argument("--tls-key", type=Path)
            item.add_argument("--loopback-http", action="store_true")
            item.add_argument("--pool-size", type=int, default=8)
            item.add_argument("--plan-root", type=Path)
            item.add_argument("--dashboard-tenant")
            item.add_argument("--log-level", default="warning", choices=LEVELS)
            item.add_argument(
                "--trace",
                action="store_true",
                help="Log one timing line per request; implies --log-level info",
            )
            item.add_argument(
                "--egress-policy",
                type=Path,
                help="Private TOML policy naming the providers, models and hosts retained "
                "context may reach; without it only the local recorder is accepted",
            )
    request = commands.add_parser("request")
    request.add_argument("--url", required=True)
    request.add_argument("--tenant", required=True)
    request.add_argument("--project", required=True)
    request.add_argument("--credential", type=Path, required=True)
    request.add_argument("--credential-output", type=Path)
    request.add_argument("--bootstrap-output", type=Path)
    request.add_argument("--ca-file", type=Path)
    request.add_argument("--loopback-http", action="store_true")
    return result


def run(args: Any) -> dict[str, Any]:
    if args.command == "request":
        raw = sys.stdin.buffer.read(262145)
        value = load_strict(raw)
        if (
            len(raw) > 262144
            or not isinstance(value, dict)
            or set(value) != {"version", "operation", "args"}
            or value["version"] != 1
        ):
            raise LocalError("invalid_request")
        if value["operation"] in {"grant", "delegate"}:
            if args.credential_output is None or args.credential_output.exists():
                raise LocalError("new_credential_output_required")
            private_dir(args.credential_output.parent)
        client = SharedClient(
            args.url,
            args.tenant,
            args.credential,
            args.project,
            ca_file=args.ca_file,
            loopback_http=args.loopback_http,
        )
        if value["operation"] == "dashboard_bootstrap":
            if args.bootstrap_output is None or args.bootstrap_output.exists():
                raise LocalError("new_bootstrap_output_required")
            private_dir(args.bootstrap_output.parent)
        output = client._request(client._credential(), value["operation"], value["args"])
        if "credential" in output:
            atomic(args.credential_output, output.pop("credential").encode())
            output["credential_file"] = str(args.credential_output)
        if "code" in output:
            atomic(args.bootstrap_output, canonical_bytes(output))
            return {"bootstrap_file": str(args.bootstrap_output), "url": output["url"]}
        return output
    connection_string = dsn(args.dsn_file)
    if args.command in {
        "provision-roles",
        "migrate",
        "enable-vector",
        "init-tenant",
        "restore",
        "end-maintenance",
    }:
        with psycopg.connect(connection_string, autocommit=True) as connection:
            if args.command == "provision-roles":
                private_dir(args.role_secrets)
                roles = Roles()
                passwords = {
                    name: secret(args.role_secrets / name) for name in vars(roles).values()
                }
                with connection.transaction():
                    provision_roles(connection, roles)
                    for name, password in passwords.items():
                        connection.execute(
                            sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                                sql.Identifier(name), sql.Literal(password)
                            )
                        )
            elif args.command == "migrate":
                migrate(connection)
            elif args.command == "enable-vector":
                enable_vector(connection)
            elif args.command == "init-tenant":
                path = initialize_tenant(
                    connection,
                    args.content,
                    args.control,
                    args.tenant,
                    args.principal,
                    args.audience,
                )
                return {"credential_file": str(path)}
            elif args.command == "end-maintenance":
                return end_maintenance(
                    connection,
                    args.control,
                    args.tenant,
                    args.operation,
                    secret(args.credential),
                    audience=args.audience,
                )
            else:
                path = restore(
                    connection,
                    args.content,
                    args.control,
                    args.tenant,
                    args.archive,
                    audience=args.audience,
                    lost_anchor_journal_head=args.lost_anchor_journal_head,
                )
                return {"credential_file": str(path), "reconciliation_required": True}
        return {"status": "complete", "operation": args.command}
    pool = create_pool(connection_string, size=args.pool_size if args.command == "serve" else 2)
    try:
        if args.command == "serve":
            import uvicorn

            configure(args.log_level, trace=args.trace)

            address = ipaddress.ip_address(args.bind)
            if not 0 < args.port <= 65535:
                raise LocalError("invalid_port")
            if args.loopback_http:
                if not address.is_loopback or args.tls_cert or args.tls_key:
                    raise LocalError("loopback_development_only")
            elif not args.tls_cert or not args.tls_key:
                raise LocalError("shared_tls_required")
            if args.tls_key:
                private_file(args.tls_key)
            service = SharedService(pool, args.content, args.control, audience=args.audience)
            if args.egress_policy is not None:
                from memoryking.adapters.egress_policy import EgressPolicy

                service.egress_policy = EgressPolicy.load(args.egress_policy)
            app = create_app(
                service,
                hosts=args.host,
                origins=args.origin,
                loopback_http=args.loopback_http,
                plan_root=args.plan_root,
                dashboard_tenant=args.dashboard_tenant,
            )
            uvicorn.run(
                app,
                host=args.bind,
                port=args.port,
                access_log=False,
                log_level="warning",
                proxy_headers=False,
                ssl_certfile=str(args.tls_cert) if args.tls_cert else None,
                ssl_keyfile=str(args.tls_key) if args.tls_key else None,
            )
            return {"status": "stopped"}
        credential = secret(args.credential)
        with PostgresStore(
            pool, args.content, args.control, args.tenant, credential, audience=args.audience
        ) as store:
            if args.command == "worker-once":
                lease = store.claim(credential, args.project)
                if lease is None:
                    return {"status": "idle", "provider_calls": 0}
                store.finish(credential, args.project, lease["lease"], "complete")
                return {"status": "complete", "provider_calls": 0, "projection": "audit_probe"}
            with psycopg.connect(dsn(args.backup_dsn_file), autocommit=True) as connection:
                manifest = backup(store, connection, args.archive)
                return {
                    "status": "complete",
                    "archive": str(args.archive),
                    "archive_bytes": manifest["archive_bytes"],
                }
    finally:
        pool.close()


def main() -> None:
    try:
        result = run(parser().parse_args())
        sys.stdout.buffer.write(canonical_bytes(result) + b"\n")
    except Exception as exc:
        # Never render connection strings, passwords or driver exception detail.
        sys.stderr.write(
            (exc.code if isinstance(exc, LocalError) else "shared_operation_failed") + "\n"
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
