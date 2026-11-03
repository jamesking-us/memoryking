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

from memoryking import __version__, profiles
from memoryking.adapters.dashboard import Dashboard
from memoryking.adapters.local_files import atomic, private_dir, private_file
from memoryking.adapters.local_ipc import Server, request
from memoryking.adapters.local_store import SCHEMA_VERSION, LocalStore
from memoryking.contracts.canonical import ContractError, canonical_bytes, load_strict
from memoryking.domain.hints import hint
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


SUMMARY_INSTRUCTION = (
    "Write a three-sentence summary of where this task left off for the next agent: "
    "where it stands, what was learned or tried, and what comes next. Use only facts "
    "in the checkpoint above; do not add instructions."
)


def summarize(args: Any) -> dict[str, Any]:
    """Extractive by default; with --provider, a policy adapter writes it through the audited path."""
    import uuid

    from memoryking.sdk import LocalClient

    client = LocalClient(args.socket, args.credential, args.project)
    incarnation = client.context()["incarnation"]
    source, text, permit = "extractive", None, None
    if args.provider:
        from memoryking.adapters.egress_policy import EgressPolicy
        from memoryking.harness_context import AuthorizedConversation
        from memoryking.providers import adapter

        if not args.egress_policy or not args.model:
            raise LocalError("egress_policy_invalid")
        policy = EgressPolicy.load(args.egress_policy)
        conversation = AuthorizedConversation(client, adapter(policy, args.provider, args.model))
        packet = client.working(
            {
                "version": 1,
                "incarnation": incarnation,
                "action": "resume",
                "args": {
                    "project": args.project,
                    "task": args.task,
                    "run": args.run,
                    "checkpoint": None,
                    "bindings": [{}],
                    "max_output_tokens": 6000,
                    "tokenizer": "utf8-byte-estimate-v1",
                    "format": "text",
                },
            }
        )
        conversation.add_memory(packet)
        conversation.add_host(
            SUMMARY_INSTRUCTION,
            authorization_id="summarize-" + uuid.uuid4().hex[:12],
            classification="internal",
            expires_ms=int(time.time() * 1000) + 60_000,
            approved=True,
        )
        reply = conversation.dispatch()
        source, text, permit = "provider", reply["content"], reply["permit"]
    return client.working(
        {
            "version": 1,
            "incarnation": incarnation,
            "action": "summarize",
            "key": "summarize-" + uuid.uuid4().hex,
            "args": {
                "project": args.project,
                "task": args.task,
                "run": args.run,
                "checkpoint": None,
                "source": source,
                "text": text,
                "permit": permit,
                "readers": None,
            },
        }
    )


def render_audit(result: dict[str, Any]) -> str:
    """A human table of egress records; hashes are shortened, nothing else is."""
    lines = [f"egress policy {result['policy_revision']}"]
    items = result["items"]
    if not items:
        lines.append("no egress records in the retention window")
        return "\n".join(lines)
    header = (
        "when (utc)",
        "status",
        "provider",
        "model",
        "host",
        "class",
        "bytes",
        "receipts",
        "permit",
    )
    rows = [
        (
            time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(item["evaluated_ms"] / 1000)),
            item["status"],
            item["provider"],
            item["model"],
            item["host"],
            item["classification"],
            str(item["request_bytes"]),
            str(len(item["receipts"])),
            item["permit"],
        )
        for item in items
    ]
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    for row in [header, *rows]:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    if result.get("next_before") is not None:
        lines.append(f"more: --before {result['next_before']}")
    return "\n".join(lines)


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
    store: Path,
    control: Path,
    socket: Path | None,
    credential: Path | None,
    embedding_model: Path | None = None,
    backup_dir: Path | None = None,
    backup_every: str | None = None,
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
                "ok" if version == SCHEMA_VERSION else "warn",
                "schema",
                f"SQLite schema {version}"
                + (
                    ""
                    if version == SCHEMA_VERSION
                    else f"; the daemon migrates to {SCHEMA_VERSION} on next start"
                ),
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
    from importlib.util import find_spec

    if find_spec("onnxruntime") is not None:
        record("ok", "embeddings runtime", "onnxruntime installed")
    else:
        record(
            "warn",
            "embeddings runtime",
            "not installed; `pip install --require-hashes -r requirements-embeddings.lock` enables local semantic search",
        )
    if embedding_model is not None:
        try:
            from memoryking.adapters.local_embeddings import model_files

            model_files(embedding_model)
            record("ok", "embedding model", f"{embedding_model} verified")
        except (LocalError, OSError) as exc:
            code = exc.code if isinstance(exc, LocalError) else type(exc).__name__
            record(
                "fail",
                "embedding model",
                f"{embedding_model}: {code}; run `python -m memoryking.model_install --destination DIR`",
            )
    else:
        record(
            "warn",
            "embedding model",
            "no model configured; `serve --embedding-model DIR` after installing one enables semantic search",
        )
    from memoryking.backups import check as backup_check

    level, detail = backup_check(backup_dir, backup_every, int(time.time() * 1000))
    record(level, "backups", detail)
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


def render_influence(report: dict[str, Any]) -> str:
    """A reverse-dependency report as readable text: what was built from the record."""
    counts = report["counts"]

    def stamp(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")

    lines = [
        f"Observation {report['observation']} from {report['source']} by {report['captured_by']}",
        f"Reached {counts['principals']} principal(s): {', '.join(report['principals']) or 'none'}",
        "",
    ]
    for title, key, render in (
        (
            "Claims citing it",
            "claims",
            lambda c: f"{c['revision']} ({c['kind']} by {c['issuer']}, family {c['family']})",
        ),
        (
            "Checkpoints built on it",
            "checkpoints",
            lambda c: (
                f"{c['checkpoint']} task {c['task']} rev {c['revision']} by {c['writer']} for {', '.join(c['readers'])} at {stamp(c['created_ms'])}"
            ),
        ),
        (
            "Handoffs that carried it",
            "handoffs",
            lambda h: (
                f"{h['handoff']} {h['sender']} -> {h['recipient']} ({h['state']}) at {stamp(h['created_ms'])}"
            ),
        ),
        (
            "Summaries derived from it",
            "summaries",
            lambda s: f"{s['summary']} of {s['checkpoint']} by {s['principal']} ({s['method']})",
        ),
        (
            "Deliveries that included it",
            "deliveries",
            lambda d: f"{d['receipt']} {d['kind']} to {d['principal']} at {stamp(d['created_ms'])}",
        ),
        ("Experience records", "experience", lambda x: x["record"]),
    ):
        items = report[key]
        lines.append(
            f"{title}: {len(items)}" + (" (truncated)" if key in report["truncation"] else "")
        )
        lines.extend(f"  - {render(item)}" for item in items)
    lines += ["", f"Coverage: {report['coverage'].replace('_', ' ').replace(';', ';')}"]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"memoryking {__version__}")
    parser.add_argument(
        "--profile",
        default=os.environ.get("MEMORYKING_PROFILE"),
        help="Named store from the profiles config; fills omitted --store, --control, "
        "--socket and --credential (MEMORYKING_PROFILE)",
    )
    parser.add_argument(
        "--config", type=Path, help="Profiles file; default ~/.memoryking/config.toml"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prof = commands.add_parser("profile", help="Name a store so commands need no paths")
    prof_commands = prof.add_subparsers(dest="profile_command", required=True)
    adder = prof_commands.add_parser("add")
    adder.add_argument("name")
    adder.add_argument("--store", type=Path, required=True)
    adder.add_argument("--control", type=Path, required=True)
    adder.add_argument("--credential", type=Path, help="Default admin credential file")
    adder.add_argument("--socket", type=Path, help="Default: STORE/service.sock")
    adder.add_argument("--dashboard-port", type=int)
    adder.add_argument("--embedding-model", type=Path, help="Installed local model directory")
    adder.add_argument("--backup-dir", type=Path)
    adder.add_argument("--backup-every", help="Such as 12h or 1d")
    adder.add_argument("--backup-keep", type=int)
    prof_commands.add_parser("list")
    prof_commands.add_parser("remove").add_argument("name")
    for name in ("init", "serve", "backup", "restore", "gc", "control-checkpoint"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--store", type=Path)
        cmd.add_argument("--control", type=Path)
        if name == "serve":
            cmd.add_argument(
                "--dashboard-port",
                type=int,
                help="Opt-in loopback port; 0 chooses a free port; a profile may set it",
            )
            cmd.add_argument("--backup-dir", type=Path, help="Directory for scheduled backups")
            cmd.add_argument("--backup-every", help="Backup interval such as 12h or 1d")
            cmd.add_argument(
                "--backup-keep", type=int, help="Complete backups to retain; default 14"
            )
            cmd.add_argument(
                "--embedding-model",
                type=Path,
                help="Installed local model directory; the daemon then embeds queries and "
                "indexes every enabled project itself (a profile may set it)",
            )
            cmd.add_argument(
                "--log-level",
                default="warning",
                choices=LEVELS,
                help="Redacted operator log level on stderr; request ids link replies to lines",
            )
            cmd.add_argument(
                "--trace",
                action="store_true",
                help="Log one timing line per request (operation, action, outcome, elapsed, "
                "lock wait, transaction time, response size); implies --log-level info",
            )
            cmd.add_argument(
                "--egress-policy",
                type=Path,
                help="Private TOML policy naming the providers, models and hosts retained "
                "context may reach; without it only the local recorder is accepted",
            )
            cmd.add_argument(
                "--summary-idle",
                default="30m",
                help="Derive a 'where this left off' summary for a task idle this long "
                "(e.g. 30m, 2h); 0 disables",
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
    status.add_argument("--socket", type=Path)
    status.add_argument("--credential", type=Path)
    status.add_argument("--project", default="main")
    status.add_argument("--json", action="store_true", help="Print the raw status result")
    doc = commands.add_parser("doctor", help="Check the installation and report fixes")
    doc.add_argument("--store", type=Path)
    doc.add_argument("--control", type=Path)
    doc.add_argument("--socket", type=Path, help="Also query the running daemon")
    doc.add_argument("--credential", type=Path, help="Admin credential for the daemon query")
    doc.add_argument("--embedding-model", type=Path, help="Model directory to verify")
    doc.add_argument("--backup-dir", type=Path, help="Scheduled backup directory to check")
    doc.add_argument("--backup-every", help="Backup policy interval the directory should meet")
    cred = commands.add_parser("credential", help="Issue, list and revoke agent credentials")
    cred_commands = cred.add_subparsers(dest="credential_command", required=True)
    for name in ("issue", "list", "revoke"):
        item = cred_commands.add_parser(name)
        item.add_argument("--socket", type=Path)
        item.add_argument("--credential", type=Path, help="Admin credential")
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
    summ = commands.add_parser(
        "summarize", help="Derive a 'where this left off' summary of a task, with its own receipt"
    )
    summ.add_argument("--socket", type=Path)
    summ.add_argument("--credential", type=Path)
    summ.add_argument("--project", required=True)
    summ.add_argument("--task", required=True)
    summ.add_argument("--run")
    summ.add_argument("--egress-policy", type=Path, help="With --provider: the daemon's policy")
    summ.add_argument("--provider", help="Write the summary with this policy adapter")
    summ.add_argument("--model", help="A model the policy allows for that provider")
    summ.add_argument("--json", action="store_true")
    audit = commands.add_parser(
        "egress-audit",
        help="List what left this store for a model provider, and whether it arrived",
    )
    audit.add_argument("--socket", type=Path)
    audit.add_argument("--credential", type=Path, help="Credential with inference capability")
    audit.add_argument("--project", required=True)
    audit.add_argument("--before", type=int, help="Page further back from this evaluated_ms")
    audit.add_argument("--json", action="store_true")
    dash = commands.add_parser("dashboard", help="Open the dashboard in a browser")
    dash_commands = dash.add_subparsers(dest="dashboard_command", required=True)
    opener = dash_commands.add_parser("open")
    opener.add_argument("--socket", type=Path)
    opener.add_argument("--credential", type=Path)
    opener.add_argument(
        "--print-only", action="store_true", help="Print the sign-in link instead of opening it"
    )
    emb = commands.add_parser("embeddings", help="Enable or inspect local semantic search")
    emb_commands = emb.add_subparsers(dest="embeddings_command", required=True)
    for name in ("enable", "status"):
        item = emb_commands.add_parser(name)
        item.add_argument("--socket", type=Path)
        item.add_argument("--credential", type=Path, help="Admin credential")
        item.add_argument("--project", required=True)
    cap = commands.add_parser("capture", help="Save one observation from text or stdin")
    cap.add_argument("--socket", type=Path)
    cap.add_argument("--credential", type=Path)
    cap.add_argument("--project", required=True)
    cap.add_argument("--text", help="The observation; omit to read it from stdin")
    cap.add_argument("--source", default="cli", help="Where it came from, e.g. git or pytest")
    cap.add_argument("--event", help="Stable event id so a rerun replays; default: a text hash")
    cap.add_argument(
        "--readers", help="Comma-separated principals; default everyone in the project"
    )
    cap.add_argument(
        "--classification", default="internal", choices=("public", "internal", "restricted")
    )
    cap.add_argument(
        "--observed-ms",
        type=int,
        help="Observation time in Unix ms; default is today to the day so a rerun replays",
    )
    rec = commands.add_parser("recall", help="Print a cited Markdown briefing for a query")
    rec.add_argument("--socket", type=Path)
    rec.add_argument("--credential", type=Path)
    rec.add_argument("--project", required=True)
    rec.add_argument("--query", required=True)
    rec.add_argument("--budget", type=int, default=6000)
    rec.add_argument("--task")
    rec.add_argument(
        "--as-of", help="Brief as the project stood then: a date, ISO date-time, or Unix ms"
    )
    rec.add_argument("--json", action="store_true", help="Print the structured packet instead")
    infl = commands.add_parser(
        "influence", help="Where one observation went: every record built from it and who got it"
    )
    infl.add_argument("observation")
    infl.add_argument("--socket", type=Path)
    infl.add_argument("--credential", type=Path)
    infl.add_argument("--project", required=True)
    infl.add_argument("--json", action="store_true", help="Print the structured report instead")
    tr = commands.add_parser("trust", help="Source trust: hold captures from untrusted sources")
    tr_commands = tr.add_subparsers(dest="trust_command", required=True)
    for name, text in (
        ("set", "Mark a source (exact name or glob) trusted or untrusted for a project or *"),
        ("list", "Show the trust rules that apply to a project"),
        ("held", "List held captures awaiting review in a project"),
        ("release", "Release one held capture so reads, briefings and reviews may use it"),
    ):
        sub = tr_commands.add_parser(name, help=text)
        sub.add_argument("--socket", type=Path)
        sub.add_argument("--credential", type=Path)
        sub.add_argument("--project", required=True, help="Project, or * for every project on set")
        sub.add_argument("--json", action="store_true")
        if name == "set":
            sub.add_argument("--source", required=True)
            sub.add_argument("--level", required=True, choices=("trusted", "untrusted"))
            sub.add_argument("--key", help="Retry key; default: derived from the rule")
        if name == "release":
            sub.add_argument("observation")
            sub.add_argument("--key", help="Retry key; default: derived from the observation")
    hk = commands.add_parser(
        "hook", help="Run as a host hook: read the host's event JSON on stdin, never fail"
    )
    hk.add_argument("kind", choices=("session-start", "pre-compact", "session-end"))
    hk.add_argument("--host", choices=("claude", "codex", "grok"), help="Host that is calling")
    hk.add_argument("--socket", type=Path)
    hk.add_argument("--credential", type=Path)
    hk.add_argument("--project", help="Default: MEMORYKING_PROJECT")
    hk.add_argument("--task", help="Resume this task at start; checkpoint it before loss")
    hk.add_argument("--query", help="Briefing query at session start; default: task or project")
    hk.add_argument("--budget", type=int, default=6000)
    hk.add_argument(
        "--strict", action="store_true", help="Exit non-zero on error instead of only logging"
    )
    hks = commands.add_parser("hooks", help="Install or remove the host hooks")
    hks_commands = hks.add_subparsers(dest="hooks_command", required=True)
    inst = hks_commands.add_parser("install", help="Write the three hooks into a host's config")
    inst.add_argument("--host", required=True, choices=("claude", "codex", "grok"))
    inst.add_argument("--scope", default="project", choices=("project", "user"))
    inst.add_argument("--project", required=True)
    inst.add_argument("--socket", type=Path)
    inst.add_argument("--credential", type=Path)
    inst.add_argument("--task", help="Task the hooks resume and checkpoint")
    inst.add_argument("--query", help="Fixed briefing query for session start")
    inst.add_argument("--cwd", type=Path, help="Project directory; default: current")
    inst.add_argument("--print", action="store_true", help="Show the merged config, write nothing")
    inst.add_argument("--remove", action="store_true", help="Remove MemoryKing's hooks")
    rpc = commands.add_parser(
        "request", help="Read a versioned operation/args JSON object from stdin"
    )
    rpc.add_argument("--socket", type=Path)
    rpc.add_argument("--credential", type=Path)
    rpc.add_argument(
        "--credential-output", type=Path, help="Required for grant; saves the new secret privately"
    )
    login = commands.add_parser(
        "dashboard-login", help="Save a one-use browser bootstrap code privately"
    )
    login.add_argument("--socket", type=Path)
    login.add_argument("--credential", type=Path)
    login.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "profile":
            if args.profile_command == "add":
                values: dict[str, Any] = {
                    "store": str(args.store.resolve()),
                    "control": str(args.control.resolve()),
                }
                for field in ("credential", "socket", "embedding_model", "backup_dir"):
                    if getattr(args, field) is not None:
                        values[field] = str(getattr(args, field).resolve())
                for field in ("dashboard_port", "backup_every", "backup_keep"):
                    if getattr(args, field) is not None:
                        values[field] = getattr(args, field)
                path = profiles.save(args.name, values, args.config)
                print(json.dumps({"profile": args.name, "config": str(path)}))
            elif args.profile_command == "remove":
                path = profiles.remove(args.name, args.config)
                print(json.dumps({"removed": args.name, "config": str(path)}))
            else:
                for name, values in sorted(profiles.load(args.config).items()):
                    print(name)
                    for field, value in values.items():
                        print(f"  {field} = {value}")
            return 0
        profiles.apply(args, args.profile, args.config)
        optional = (
            {"socket", "credential"}
            if args.command
            in {"doctor", "capture", "recall", "hook", "hooks", "influence", "trust"}
            else set()
        )
        for field in ("store", "control", "socket", "credential"):
            if hasattr(args, field) and getattr(args, field) is None and field not in optional:
                raise LocalError(field + "_required")
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
        elif args.command == "embeddings":
            from memoryking.adapters.local_embeddings import model_manifest
            from memoryking.domain.embeddings import fingerprint

            token = read_credential(args.credential)
            context = request(args.socket, token, "context", {"project": args.project})
            fp = fingerprint(model_manifest())

            def embedding(action: str, extra: dict[str, Any], key: str | None = None) -> Any:
                command: dict[str, Any] = {
                    "version": 1,
                    "incarnation": context["incarnation"],
                    "action": action,
                    "args": {"project": args.project, **extra},
                }
                if key:
                    command["key"] = key
                return request(args.socket, token, "embedding", {"command": command})

            if args.embeddings_command == "status":
                print(json.dumps(embedding("status", {"fingerprint": fp})))
            else:
                try:
                    current = embedding("status", {"fingerprint": fp})
                except LocalError as exc:
                    if exc.code != "embedding_partition_unavailable":
                        raise
                    current = {}
                generation = int(current.get("generation") or 0)
                if current.get("state") == "experimental":
                    print(json.dumps({**current, "already_enabled": True}))
                else:
                    result = embedding(
                        "configure",
                        {
                            "model": "minilm-l6-v2-onnx",
                            "state": "experimental",
                            "expected_generation": generation,
                        },
                        key="enable-" + str(generation),
                    )
                    print(json.dumps(result))
        elif args.command == "capture":
            from memoryking import hooks

            text = args.text if args.text is not None else sys.stdin.read()
            result = hooks.capture(
                text,
                source=args.source,
                event=args.event,
                readers=args.readers.split(",") if args.readers else None,
                classification=args.classification,
                observed_ms=args.observed_ms,
                socket=args.socket,
                credential=args.credential,
                project=args.project,
            )
            print(json.dumps({k: result[k] for k in ("observation", "position") if k in result}))
        elif args.command == "recall":
            from memoryking import hooks

            packet = hooks.recall(
                args.query,
                budget=args.budget,
                text=not args.json,
                task=args.task,
                as_of=hooks.as_of_ms(args.as_of) if args.as_of else None,
                socket=args.socket,
                credential=args.credential,
                project=args.project,
            )
            if args.json:
                sys.stdout.buffer.write(canonical_bytes(packet) + b"\n")
            else:
                print(packet["text"], end="")
        elif args.command == "hook":
            from memoryking import host_hooks

            try:
                raw = "" if sys.stdin.isatty() else sys.stdin.read()
                payload = host_hooks.read_event(raw)
                where = {
                    "socket": args.socket,
                    "credential": args.credential,
                    "project": args.project,
                    "profile": args.profile,
                    "config": args.config,
                }
                if args.kind == "session-start":
                    sys.stdout.write(
                        host_hooks.session_start(
                            payload, query=args.query, task=args.task, budget=args.budget, **where
                        )
                    )
                else:
                    summary = host_hooks.save(
                        payload, args.kind, host=args.host, task=args.task, **where
                    )
                    print(json.dumps(summary), file=sys.stderr)
            except (LocalError, OSError, ValueError, KeyError) as exc:
                code = exc.code if isinstance(exc, LocalError) else type(exc).__name__
                print(f"memoryking hook {args.kind}: {code}", file=sys.stderr)
                if args.strict:
                    return 1
            return 0
        elif args.command == "hooks":
            from memoryking import host_hooks

            result = host_hooks.install(
                host=args.host,
                scope=args.scope,
                project=args.project,
                cwd=(args.cwd or Path.cwd()).resolve(),
                home=Path.home(),
                profile=args.profile,
                socket=args.socket if args.profile is None else None,
                credential=args.credential if args.profile is None else None,
                task=args.task,
                query=args.query,
                remove=args.remove,
                write=not args.print,
            )
            if args.print:
                print(json.dumps(result["config"], indent=2))
            else:
                print(
                    json.dumps(
                        {k: result[k] for k in ("host", "path", "events", "removed", "note")}
                    )
                )
        elif args.command == "doctor":
            findings = doctor(
                args.store,
                args.control,
                args.socket,
                args.credential,
                args.embedding_model,
                args.backup_dir,
                args.backup_every,
            )
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
        elif args.command == "summarize":
            result = summarize(args)
            if args.json:
                sys.stdout.buffer.write(canonical_bytes(result) + b"\n")
            else:
                print(result["summary"])
                print(
                    f"receipt {result['receipt']} · {result['method']} · "
                    f"checkpoint {result['checkpoint']} revision {result['revision']}"
                )
        elif args.command == "trust":
            token = read_credential(args.credential)
            context = request(args.socket, token, "context", {"project": args.project})
            command: dict[str, Any] = {
                "version": 1,
                "incarnation": context["incarnation"],
                "action": {
                    "set": "trust",
                    "list": "trust_list",
                    "held": "held",
                    "release": "release",
                }[args.trust_command],
                "args": {"project": args.project},
            }
            if args.trust_command == "set":
                command["args"].update(source=args.source, level=args.level)
                command["key"] = args.key or f"trust-{args.project}-{args.source}-{args.level}"
            elif args.trust_command == "release":
                command["args"]["observation"] = args.observation
                command["key"] = args.key or f"release-{args.observation}"
            report = request(args.socket, token, "inspection", {"command": command})
            if args.json:
                sys.stdout.buffer.write(canonical_bytes(report) + b"\n")
            elif args.trust_command == "list":
                for rule in report["rules"]:
                    print(
                        f"{rule['project']:<16} {rule['source']:<32} {rule['level']} (by {rule['decided_by']})"
                    )
                if not report["rules"]:
                    print("No trust rules; every source is trusted.")
            elif args.trust_command == "held":
                for item in report["held"]:
                    print(
                        f"{item['observation']} from {item['source']} by {item['captured_by']}: {item['preview']!r}"
                    )
                if not report["held"]:
                    print("No held captures.")
            else:
                print(json.dumps(report))
        elif args.command == "influence":
            token = read_credential(args.credential)
            context = request(args.socket, token, "context", {"project": args.project})
            report = request(
                args.socket,
                token,
                "inspection",
                {
                    "command": {
                        "version": 1,
                        "incarnation": context["incarnation"],
                        "action": "influence",
                        "args": {"project": args.project, "observation": args.observation},
                    }
                },
            )
            if args.json:
                sys.stdout.buffer.write(canonical_bytes(report) + b"\n")
            else:
                print(render_influence(report), end="")
        elif args.command == "egress-audit":
            result = request(
                args.socket,
                read_credential(args.credential),
                "inference",
                {
                    "command": {
                        "version": 1,
                        "action": "audit",
                        "args": {"project": args.project, "before": args.before},
                    }
                },
            )
            if args.json:
                sys.stdout.buffer.write(canonical_bytes(result) + b"\n")
            else:
                print(render_audit(result))
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
                    configure(args.log_level, trace=args.trace)
                    if args.egress_policy is not None:
                        from memoryking.adapters.egress_policy import EgressPolicy

                        store.egress_policy = EgressPolicy.load(args.egress_policy)
                    store.summary_idle_ms = (
                        0 if args.summary_idle in {"0", "off"} else duration_ms(args.summary_idle)
                    )

                    def stop(signum: int, frame: object) -> None:
                        raise KeyboardInterrupt

                    signal.signal(signal.SIGTERM, stop)
                    dashboard = None
                    embeddings = None
                    if args.backup_dir is not None:
                        from memoryking.backups import Policy

                        store.backup_policy = Policy(
                            args.backup_dir, args.backup_every or "1d", args.backup_keep or 14
                        )
                    if args.embedding_model is not None:
                        from memoryking.adapters.local_embeddings import LocalOnnxEmbedding
                        from memoryking.embedding_loop import EmbeddingLoop

                        store.embedder = LocalOnnxEmbedding(args.embedding_model)
                    if args.dashboard_port is not None:
                        if not 0 <= args.dashboard_port <= 65535:
                            raise LocalError("invalid_dashboard_configuration")
                        dashboard = Dashboard(store, args.dashboard_port)
                        store.dashboard = dashboard
                        dashboard.start()
                    try:
                        with Server(store) as server:
                            print(
                                json.dumps({"ready": True, "socket": str(server.path)}), flush=True
                            )
                            event("serve", "ready", dashboard=dashboard is not None)
                            if store.embedder is not None:
                                embeddings = EmbeddingLoop(
                                    store,
                                    server.path,
                                    args.control / "owner.credential",
                                    store.embedder,
                                )
                                embeddings.start()
                            server.serve_forever(poll_interval=0.2)
                    except KeyboardInterrupt:
                        pass
                    finally:
                        if embeddings is not None:
                            embeddings.stop()
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
            json.dumps(
                {"error": code, "hint": hint(code)}
                if (code := exc.code if isinstance(exc, LocalError) else "invalid_request")
                else {}
            ),
            file=sys.stderr,
        )
        return 2
    except (OSError, ValueError):
        code = "local_io_or_configuration_error"
        print(json.dumps({"error": code, "hint": hint(code)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
