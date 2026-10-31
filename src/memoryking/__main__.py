"""MemoryKing local service CLI. Administrative recovery requires an offline owner."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memoryking import __version__
from memoryking.adapters.dashboard import Dashboard
from memoryking.adapters.local_files import atomic, private_dir, private_file
from memoryking.adapters.local_ipc import Server, request
from memoryking.adapters.local_store import LocalStore
from memoryking.contracts.canonical import ContractError, canonical_bytes, load_strict
from memoryking.domain.local import LocalError
from memoryking.observability import LEVELS, configure, event


def bar(used: int, limit: int, width: int = 12) -> str:
    filled = min(width, round(width * used / limit)) if limit else 0
    return "[" + "#" * filled + "." * (width - filled) + "]"


def render_status(result: dict[str, Any], version: str) -> str:
    """A human summary of the status operation; `--json` prints the raw result instead."""
    projection = result.get("projection", {})
    jobs = result.get("jobs", {})
    lines = [
        f"MemoryKing {version}",
        f"Store      {result['store']}",
        f"History    {result['incarnation']}",
        f"Position   {result['position']}",
        "Frontier   "
        f"{projection.get('frontier', '?')} / {result['position']}"
        f" (projection {projection.get('channel', '?')} generation {projection.get('generation', '?')})",
        "Jobs       "
        + (
            " · ".join(f"{state} {count}" for state, count in sorted(jobs.items()))
            if jobs
            else "none"
        ),
    ]
    capacity = result.get("capacity", {})
    if capacity:
        lines.append("Capacity")
        for name, value in capacity.items():
            used, limit = value["used"], value["limit"]
            shown = (
                f"{used / 1048576:.1f} / {limit // 1048576} MiB"
                if name == "rendered_bytes"
                else f"{used:,} / {limit:,}"
            )
            flag = "  warning" if value.get("warning") else ""
            lines.append(f"  {name:<20}{shown:>22}  {bar(used, limit)}{flag}")
    if result.get("satisfied") is not None:
        lines.append(f"Watermark  {'satisfied' if result['satisfied'] else 'not yet reached'}")
    return "\n".join(lines)


DURATION_UNITS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}


def duration_ms(text: str) -> int:
    """`45m`, `12h` or `30d` to milliseconds; anything else is invalid."""
    if len(text) < 2 or text[-1] not in DURATION_UNITS or not text[:-1].isdigit():
        raise LocalError("invalid_duration")
    return int(text[:-1]) * DURATION_UNITS[text[-1]]


def render_grants(items: list[dict[str, Any]]) -> str:
    if not items:
        return "No grants."
    rows = [("PRINCIPAL", "PROJECT", "CAPABILITIES", "VALID", "EXPIRES")]
    for item in items:
        expires = item["expires"]
        rows.append(
            (
                item["principal"],
                item["project"],
                ",".join(item["capabilities"]),
                "yes" if item["valid_now"] else "no",
                "never"
                if expires is None
                else datetime.fromtimestamp(expires / 1000, timezone.utc).strftime(
                    "%Y-%m-%d %H:%MZ"
                ),
            )
        )
    widths = [max(len(r[i]) for r in rows) for i in range(5)]
    return "\n".join("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip() for r in rows)


def read_credential(path: Path) -> str:
    private_file(path)
    return path.read_text().strip()


def doctor(
    store: Path, control: Path, socket: Path | None, credential: Path | None
) -> list[tuple[str, str, str]]:
    """Health checks an operator can run while the daemon is up. Each is (level, name, detail)."""
    import sqlite3

    from memoryking.adapters.local_files import Control, private_dir
    from memoryking.adapters.token_count import encoding

    findings: list[tuple[str, str, str]] = []

    def record(level: str, name: str, detail: str) -> None:
        findings.append((level, name, detail))

    if sys.version_info[:2] == (3, 12):
        record("ok", "python", sys.version.split()[0])
    else:
        record("fail", "python", f"{sys.version.split()[0]} found; MemoryKing needs 3.12")
    for label, path in (("store", store), ("control", control)):
        try:
            private_dir(path)
            record("ok", label + " directory", f"{path} is owner-private")
        except (LocalError, OSError) as exc:
            code = exc.code if isinstance(exc, LocalError) else type(exc).__name__
            record("fail", label + " directory", f"{path}: {code}; chmod 700 and check ownership")
    try:
        from memoryking.adapters.local_store import paths

        paths(store, control)
        record("ok", "directory layout", "store and control are independent")
    except LocalError as exc:
        record("fail", "directory layout", f"{exc.code}; neither directory may contain the other")
    sock = socket or store / "service.sock"
    if len(os.fsencode(sock)) > 100:
        record("fail", "socket path", f"{len(os.fsencode(sock))} bytes; must fit in 100")
    elif sock.exists():
        record("ok", "socket", f"{sock} present")
    else:
        record("warn", "socket", f"{sock} absent; is `memoryking serve` running?")
    database = store / "store.sqlite"
    if database.exists():
        try:
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
            record(
                "ok" if version == 9 else "warn",
                "schema",
                f"SQLite schema {version}"
                + ("" if version == 9 else "; the daemon migrates to 9 on next start"),
            )
        except sqlite3.Error as exc:
            record("fail", "schema", f"cannot read {database}: {type(exc).__name__}")
    else:
        record("fail", "schema", f"{database} missing; run `memoryking init`")
    try:
        controller = Control(control)
        anchor = controller.read_anchor()
        records = controller.journal(anchor)
        detail = f"{len(records)} records, state {anchor['state']}"
        record("ok" if anchor["state"] == "active" else "warn", "journal", detail)
    except LocalError as exc:
        record("fail", "journal", f"{exc.code}; stay offline and use the recovery guide")
    except OSError as exc:
        record("fail", "journal", f"{type(exc).__name__} reading {control}")
    try:
        encoding()
        record("ok", "tokenizer", "exact cl100k_base available")
    except LocalError:
        record("warn", "tokenizer", "exact tokenizer missing; byte estimate is used and disclosed")
    if socket is not None and credential is not None and sock.exists():
        try:
            result = request(sock, read_credential(credential), "status", {"project": "main"})
            warnings = [k for k, v in result.get("capacity", {}).items() if v.get("warning")]
            frontier = result["projection"]["frontier"]
            detail = f"position {result['position']}, frontier {frontier}"
            if warnings:
                record("warn", "capacity", ", ".join(warnings) + " at 80 percent or more")
            else:
                record("ok", "capacity", "all resources under 80 percent")
            record("ok" if frontier == result["position"] else "warn", "daemon", detail)
        except LocalError as exc:
            record("fail", "daemon", f"status request failed: {exc.code}")
        except OSError as exc:
            record("fail", "daemon", f"socket unreachable: {type(exc).__name__}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"memoryking {__version__}")
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
                "--log-level",
                default="warning",
                choices=LEVELS,
                help="Redacted operator log level on stderr; request ids link replies to lines",
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
    status = commands.add_parser(
        "status", help="Show store health, frontier, jobs and capacity through the daemon"
    )
    status.add_argument("--socket", type=Path, required=True)
    status.add_argument("--credential", type=Path, required=True)
    status.add_argument("--project", default="main")
    status.add_argument("--json", action="store_true", help="Print the raw status result")
    doc = commands.add_parser("doctor", help="Check the installation and report fixes")
    doc.add_argument("--store", type=Path, required=True)
    doc.add_argument("--control", type=Path, required=True)
    doc.add_argument("--socket", type=Path, help="Also query the running daemon")
    doc.add_argument("--credential", type=Path, help="Admin credential for the daemon query")
    cred = commands.add_parser("credential", help="Issue, list and revoke agent credentials")
    cred_commands = cred.add_subparsers(dest="credential_command", required=True)
    for name in ("issue", "list", "revoke"):
        item = cred_commands.add_parser(name)
        item.add_argument("--socket", type=Path, required=True)
        item.add_argument("--credential", type=Path, required=True, help="Admin credential")
        if name in {"issue", "revoke"}:
            item.add_argument("principal")
            item.add_argument("--project", required=True)
        if name == "issue":
            item.add_argument(
                "--capabilities", required=True, help="Comma-separated, for example capture,read"
            )
            item.add_argument("--expires", help="Validity such as 45m, 12h or 30d")
            item.add_argument("--output", type=Path, required=True, help="New private file")
        if name == "list":
            item.add_argument("--json", action="store_true")
    dash = commands.add_parser("dashboard", help="Open the dashboard in a browser")
    dash_commands = dash.add_subparsers(dest="dashboard_command", required=True)
    opener = dash_commands.add_parser("open")
    opener.add_argument("--socket", type=Path, required=True)
    opener.add_argument("--credential", type=Path, required=True)
    opener.add_argument(
        "--print-only", action="store_true", help="Print the sign-in link instead of opening it"
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
    args = parser.parse_args()
    try:
        if args.command == "init":
            path = LocalStore.initialize(args.store, args.control, args.tenant, args.principal)
            print(json.dumps({"credential_file": str(path)}))
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
        elif args.command == "doctor":
            findings = doctor(args.store, args.control, args.socket, args.credential)
            width = max(len(name) for _, name, _ in findings)
            for level, name, detail in findings:
                print(f"{level.upper():<5} {name.ljust(width)}  {detail}")
            failed = sum(level == "fail" for level, _, _ in findings)
            print(f"{len(findings)} checks, {failed} failed")
            return 1 if failed else 0
        elif args.command == "credential":
            credential = read_credential(args.credential)
            if args.credential_command == "list":
                items = request(args.socket, credential, "grants", {})["items"]
                if args.json:
                    sys.stdout.buffer.write(canonical_bytes({"items": items}) + b"\n")
                else:
                    print(render_grants(items))
            elif args.credential_command == "revoke":
                request(
                    args.socket,
                    credential,
                    "revoke",
                    {"principal": args.principal, "project": args.project},
                )
                print(json.dumps({"revoked": args.principal, "project": args.project}))
            else:
                private_dir(args.output.parent)
                if args.output.exists() or args.output.is_symlink():
                    raise LocalError("credential_output_exists")
                grant: dict[str, Any] = {
                    "principal": args.principal,
                    "project": args.project,
                    "capabilities": sorted(set(args.capabilities.split(","))),
                }
                if args.expires:
                    grant["expires"] = int(time.time() * 1000) + duration_ms(args.expires)
                result = request(args.socket, credential, "grant", grant)
                atomic(args.output, result.pop("credential").encode())
                result.update(credential_file=str(args.output), **grant)
                print(json.dumps(result))
        elif args.command == "dashboard":
            result = request(
                args.socket, read_credential(args.credential), "dashboard_bootstrap", {}
            )
            link = result["url"] + "/#code=" + result["code"]
            if args.print_only:
                print(link)
            else:
                import webbrowser

                opened = webbrowser.open(link)
                print(
                    json.dumps(
                        {
                            "url": result["url"],
                            "opened": opened,
                            "expires_ms": result["expires_ms"],
                            "note": "The one-use code is in the link fragment and expires in 60 seconds.",
                        }
                    )
                )
        elif args.command == "status":
            private_file(args.credential)
            result = request(
                args.socket,
                args.credential.read_text().strip(),
                "status",
                {"project": args.project},
            )
            if args.json:
                sys.stdout.buffer.write(canonical_bytes(result) + b"\n")
            else:
                print(render_status(result, __version__))
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
                    configure(args.log_level)

                    def stop(signum: int, frame: object) -> None:
                        raise KeyboardInterrupt

                    signal.signal(signal.SIGTERM, stop)
                    dashboard = None
                    if args.dashboard_port is not None:
                        if not 0 <= args.dashboard_port <= 65535:
                            raise LocalError("invalid_dashboard_configuration")
                        dashboard = Dashboard(store, args.plan_root, args.dashboard_port)
                        store.dashboard = dashboard
                        dashboard.start()
                    try:
                        with Server(store) as server:
                            print(
                                json.dumps({"ready": True, "socket": str(server.path)}), flush=True
                            )
                            event("serve", "ready", dashboard=dashboard is not None)
                            server.serve_forever(poll_interval=0.2)
                    except KeyboardInterrupt:
                        pass
                    finally:
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
