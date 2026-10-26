"""MemoryKing local service CLI. Administrative recovery requires an offline owner."""

from __future__ import annotations

import argparse
import json
import signal
import sys
from contextlib import nullcontext
from pathlib import Path

from memoryking.adapters.dashboard import Dashboard
from memoryking.adapters.local_files import atomic, private_dir, private_file
from memoryking.adapters.local_ipc import Server, request
from memoryking.adapters.local_store import LocalStore
from memoryking.contracts.canonical import ContractError, canonical_bytes, load_strict
from memoryking.domain.local import LocalError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "serve", "backup", "restore", "gc", "control-checkpoint"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--store", type=Path, required=True)
        cmd.add_argument("--control", type=Path, required=True)
        if name == "serve":
            cmd.add_argument(
                "--dashboard-port", type=int, help="Opt-in loopback port; 0 chooses a free port"
            )
            cmd.add_argument("--plan-root", type=Path, help="Explicit trusted plan/report checkout")
            cmd.add_argument(
                "--report-config", type=Path, help="Private optional report worker configuration"
            )
        if name == "init":
            cmd.add_argument("--tenant", default="main")
            cmd.add_argument("--principal", default="owner")
        if name in {"backup", "restore"}:
            cmd.add_argument("--backup", type=Path, required=True)
        if name == "restore":
            cmd.add_argument(
                "--lost-anchor-journal-head",
                help="Current independently retained journal head; only for a missing anchor",
            )
    rpc = commands.add_parser(
        "request", help="Read a versioned operation/args JSON object from stdin"
    )
    rpc.add_argument("--socket", type=Path, required=True)
    rpc.add_argument("--credential", type=Path, required=True)
    rpc.add_argument(
        "--credential-output", type=Path, help="Required for grant; saves the new secret privately"
    )
    login = commands.add_parser(
        "dashboard-login", help="Save a one-use browser bootstrap code privately"
    )
    login.add_argument("--socket", type=Path, required=True)
    login.add_argument("--credential", type=Path, required=True)
    login.add_argument("--output", type=Path, required=True)
    viewer = commands.add_parser(
        "viewer-admin", help="Owner-only viewer policy JSON over local IPC"
    )
    viewer.add_argument("--socket", type=Path, required=True)
    viewer.add_argument("--credential", type=Path, required=True)
    report = commands.add_parser("report", help="Scoped report command JSON over local IPC")
    report.add_argument("--socket", type=Path, required=True)
    report.add_argument("--credential", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "init":
            path = LocalStore.initialize(args.store, args.control, args.tenant, args.principal)
            print(json.dumps({"credential_file": str(path)}))
        elif args.command in {"viewer-admin", "report"}:
            private_file(args.credential)
            body = load_strict(sys.stdin.buffer.read(262145))
            if not isinstance(body, dict):
                raise LocalError("invalid_request")
            result = request(
                args.socket,
                args.credential.read_text().strip(),
                "report" if args.command == "report" else "viewer_admin",
                {"command": body},
            )
            print(canonical_bytes(result).decode())
        elif args.command == "restore":
            path = LocalStore.restore(
                args.store,
                args.control,
                args.backup,
                lost_anchor_journal_head=args.lost_anchor_journal_head,
            )
            print(json.dumps({"credential_file": str(path), "reconciliation_required": True}))
        elif args.command == "dashboard-login":
            private_file(args.credential)
            private_dir(args.output.parent)
            if args.output.exists() or args.output.is_symlink():
                raise LocalError("credential_output_exists")
            result = request(
                args.socket, args.credential.read_text().strip(), "dashboard_bootstrap", {}
            )
            atomic(args.output, json.dumps(result).encode())
            print(json.dumps({"bootstrap_file": str(args.output), "url": result["url"]}))
        elif args.command == "request":
            private_file(args.credential)
            body = load_strict(sys.stdin.buffer.read(262145))
            if not isinstance(body, dict) or set(body) != {"operation", "args"}:
                raise LocalError("invalid_request")
            if body["operation"] == "grant" and args.credential_output is None:
                raise LocalError("credential_output_required")
            if body["operation"] == "grant":
                private_dir(args.credential_output.parent)
                if args.credential_output.exists() or args.credential_output.is_symlink():
                    raise LocalError("credential_output_exists")
            result = request(
                args.socket, args.credential.read_text().strip(), body["operation"], body["args"]
            )
            if "credential" in result:
                atomic(args.credential_output, result.pop("credential").encode())
                result["credential_file"] = str(args.credential_output)
            sys.stdout.buffer.write(canonical_bytes(result) + b"\n")
        else:
            with LocalStore(args.store, args.control) as store:
                if args.command == "serve":

                    def stop(signum: int, frame: object) -> None:
                        raise KeyboardInterrupt

                    signal.signal(signal.SIGTERM, stop)
                    dashboard = None
                    report_worker = None
                    if args.report_config is not None:
                        from memoryking.adapters.report_worker import Configuration, ReportWorker

                        config = Configuration.from_file(args.report_config)
                        config.attach(store)
                        report_worker = ReportWorker(config, lambda: nullcontext(store))
                    if args.dashboard_port is not None:
                        if not 0 <= args.dashboard_port <= 65535:
                            raise LocalError("invalid_dashboard_configuration")
                        dashboard = Dashboard(
                            store, args.plan_root or Path.cwd(), args.dashboard_port
                        )
                        store.dashboard = dashboard
                        dashboard.start()
                    try:
                        with Server(store) as server:
                            if report_worker:
                                report_worker.start()
                            print(
                                json.dumps({"ready": True, "socket": str(server.path)}), flush=True
                            )
                            server.serve_forever(poll_interval=0.2)
                    except KeyboardInterrupt:
                        pass
                    finally:
                        if report_worker:
                            report_worker.stop()
                        if dashboard:
                            dashboard.stop()
                elif args.command == "backup":
                    print(json.dumps(store.backup(args.backup)))
                elif args.command == "gc":
                    print(
                        json.dumps(
                            {
                                "expired_results": store.expire_results(),
                                "orphan_blobs_removed": store.gc(),
                            }
                        )
                    )
                elif args.command == "control-checkpoint":
                    print(
                        json.dumps(
                            {
                                "store": store.store_id,
                                "incarnation": store.incarnation,
                                "journal_sequence": store.anchor["journal_sequence"],
                                "journal_head": store.anchor["journal_head"],
                                "independent_retention_required": True,
                            }
                        )
                    )
        return 0
    except (LocalError, ContractError) as exc:
        print(
            json.dumps({"error": exc.code if isinstance(exc, LocalError) else "invalid_request"}),
            file=sys.stderr,
        )
        return 2
    except (OSError, ValueError):
        print('{"error":"local_io_or_configuration_error"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
