"""MemoryKing local service CLI. Administrative recovery requires an offline owner."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memoryking import __version__, daemon, profiles, session_bindings
from memoryking.adapters.dashboard import Dashboard
from memoryking.adapters.local_files import atomic, private_dir, private_file
from memoryking.adapters.local_ipc import Server, request
from memoryking.adapters.local_store import CAPABILITIES, SCHEMA_VERSION, LocalStore
from memoryking.application.lexical_index import RATE_PER_SECOND
from memoryking.contracts.canonical import ContractError, canonical_bytes, load_strict
from memoryking.domain.hints import hint
from memoryking.domain.local import LocalError, SystemClock
from memoryking.observability import LEVELS, configure, event


def bar(used: int, limit: int, width: int = 12) -> str:
    filled = min(width, round(width * used / limit)) if limit else 0
    return "[" + "#" * filled + "." * (width - filled) + "]"


def lexical_findings(db: Any, version: int) -> list[tuple[str, str, int]]:
    """(tenant, state, observations not yet in the current lexical format) from a read-only
    connection; a store older than schema 16 has every populated tenant on the old format."""
    found: list[tuple[str, str, int]] = []
    for (tenant,) in db.execute("SELECT tenant FROM tenants").fetchall():
        row = (
            db.execute(
                "SELECT state,cursor FROM lexical_state WHERE tenant=?", (tenant,)
            ).fetchone()
            if version >= 16
            else None
        )
        total = db.execute(
            "SELECT count(*) FROM observations WHERE tenant=? AND deleted=0", (tenant,)
        ).fetchone()[0]
        if row is None:
            if total:
                found.append((tenant, "legacy", total))
        elif row[0] != "current":
            left = db.execute(
                "SELECT count(*) FROM observations WHERE tenant=? AND deleted=0 AND position>?",
                (tenant, max(row[1], 0)),
            ).fetchone()[0]
            found.append((tenant, row[0], left))
        else:
            found.append((tenant, "current", 0))
    return found


def lexical_line(lexical: dict[str, Any]) -> str:
    """One line for the lexical index: state, and for a rebuild its progress and estimate."""
    line = f"{lexical.get('state', '?')} ({lexical.get('configuration', '?')})"
    if lexical.get("state") == "current":
        return line
    left, total = lexical.get("remaining_observations"), lexical.get("total_observations")
    if left is not None and total is not None:
        line += (
            f"; {lexical.get('percent_complete', 0)}% rebuilt into "
            f"{lexical.get('target_configuration', '?')} ({total - left:,} of {total:,} observations"
        )
        if left:
            line += f", about {duration(int(lexical.get('estimated_seconds', 0)) * 1000)} left"
        line += ")"
    return line + f". {lexical.get('action', '')}"


def lexical_total(database: Path, tenant: str) -> int:
    """Live observations of a tenant, from a read-only connection (for the doctor's percent)."""
    import sqlite3

    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        return int(
            db.execute(
                "SELECT count(*) FROM observations WHERE tenant=? AND deleted=0", (tenant,)
            ).fetchone()[0]
        )


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
    lexical = result.get("lexical")
    if lexical:
        lines.append("Lexical    " + lexical_line(lexical))
    capacity = result.get("capacity", {})
    if capacity:
        lines.append("Capacity")
        for name, value in capacity.items():
            used, limit = value["used"], value["limit"]
            shown = (
                f"{used / 1048576:.1f} / {limit // 1048576} MiB"
                if name in {"rendered_bytes", "checkpoint_bytes"}
                else f"{used:,} / {limit:,}"
            )
            flag = "  warning" if value.get("warning") else ""
            lines.append(f"  {name:<20}{shown:>22}  {bar(used, limit)}{flag}")
    if result.get("daemon"):
        info = result["daemon"]
        lines.append(
            f"Daemon     running{' (pid ' + str(info['pid']) + ')' if info.get('pid') else ''}"
            f", {'starts on demand' if info.get('autostart') else 'started by hand'}"
            f", log {info['log']}"
        )
    scopes = result.get("user_scopes")
    if scopes and scopes["scopes"]:
        lines.append(
            f"User memory {scopes['observations']:,} observations in {scopes['scopes']} user scope(s)"
            " (counts only; readable by each user and their agents)"
        )
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


BINDING_HELP = (
    "Where this session works, such as environment=prod; repeatable. Also "
    "MEMORYKING_BINDINGS and the named profile's bindings"
)


def add_binding(parser: Any) -> None:
    parser.add_argument(
        "--binding", action="append", default=[], metavar="KEY=VALUE", help=BINDING_HELP
    )


def failed_attempt(value: str) -> dict[str, str]:
    action, arrow, outcome = value.partition("=>")
    if not arrow or not action.strip() or not outcome.strip():
        raise argparse.ArgumentTypeError('expected "action => outcome"')
    return {"action": action.strip(), "outcome": outcome.strip()}


def lean_command(args: Any) -> dict[str, Any]:
    """Run the lean save_progress or resume tool for the CLI, through the same code."""
    from memoryking import hooks, lean
    from memoryking.adapters.stdio import LEAN_VALIDATORS
    from memoryking.sdk import LocalClient

    name = args.command.replace("-", "_")
    arguments: dict[str, Any] = {"task": args.task, "run": args.run}
    if name == "save_progress":
        arguments.update(objective=args.objective, current_state=args.state)
        for key, value in (
            ("next_actions", args.next),
            ("findings", args.finding),
            ("blockers", args.blocker),
            ("hard_constraints", args.constraint),
            ("failed_attempts", args.failed),
            ("share_with", args.share_with),
        ):
            if value:
                arguments[key] = value
    else:
        arguments["budget"] = args.budget
    if not LEAN_VALIDATORS[name].is_valid(arguments):
        raise LocalError("invalid_tool_arguments")
    socket, credential, project = hooks.connection_files(
        profile=args.profile,
        socket=args.socket,
        credential=args.credential,
        project=args.project,
        config=args.config,
    )
    bindings = session_bindings.resolve(args.binding, profile=args.profile, config=args.config)
    return lean.call(LocalClient(socket, credential, project, bindings=bindings), name, arguments)


def user_command(args: Any) -> int:
    """`memoryking user show|export|purge`: the user's own memory across projects."""
    from memoryking import vault as vault_module

    credential = read_credential(args.credential)

    def ask(**body: Any) -> dict[str, Any]:
        return request(args.socket, credential, "user", body)

    if args.user_command == "export":
        client = vault_module.connect(
            socket=args.socket, credential=args.credential, project=None, scope="user"
        )
        report = vault_module.export(args.dir, client, "user")
        if args.json:
            sys.stdout.buffer.write(canonical_bytes(report) + b"\n")
        else:
            print(
                f"Exported {report['observations']} file(s) from {report['versions']} "
                f"observation(s) and {report['claims']} claim(s) to {report['directory']}"
            )
    elif args.user_command == "purge":
        if not args.yes:
            print(
                "This erases the user memory and everything built from it, and cannot be"
                " undone. Run again with --yes.",
                file=sys.stderr,
            )
            return 2
        body = {"user": args.user} if args.user else {}
        total = 0
        while True:
            report = ask(action="purge", **body)
            total += report["purged"]
            if not report["remaining"] or not report["purged"]:
                break
        report["purged"] = total
        if args.json:
            sys.stdout.buffer.write(canonical_bytes(report) + b"\n")
        else:
            print(f"Erased {total} observation(s) and what was built from them.")
    else:
        try:
            ask(action="ensure")  # the owner is the default user; a no-op for anyone else
        except LocalError:
            pass
        report = ask(action="show")
        if args.json:
            sys.stdout.buffer.write(canonical_bytes(report) + b"\n")
        else:
            print(
                f"User memory of {report['user']}: {report['observations']} observation(s), "
                f"{report['claims']} claim(s), {report['constraints']} constraint(s), "
                f"{report['held']} held"
            )
            for agent in report["agents"]:
                print(f"  acts for you: {agent['principal']} ({','.join(agent['capabilities'])})")
    return 0


def summarize(args: Any) -> dict[str, Any]:
    """Extractive by default; with --provider, a policy adapter writes it through the audited path."""
    import uuid

    from memoryking.sdk import LocalClient

    client = LocalClient(
        args.socket,
        args.credential,
        args.project,
        bindings=session_bindings.resolve(args.binding, profile=args.profile, config=args.config),
    )
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
                    "bindings": client.bindings,
                    "max_output_tokens": 6000,
                    "tokenizer": "chars-estimate-v1",
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


def consolidate_command(args: Any) -> dict[str, Any]:
    """One consolidation pass, or with --watch a loop that runs until interrupted."""
    import tempfile

    from memoryking import consolidation
    from memoryking.sdk import LocalClient

    scratch = Path(tempfile.mkdtemp(prefix="mk-cons-"))
    os.chmod(scratch, 0o700)

    def make(project: str) -> tuple[Any, Any, str | None]:
        client = LocalClient(args.socket, args.credential, project)
        conversation, ceiling = consolidation.build_conversation(
            client, args.egress_policy, args.provider, args.model, scratch / "rec"
        )
        return client, conversation, ceiling

    if args.watch:
        loop = consolidation.ConsolidationLoop(
            make,
            [args.project],
            every_s=duration_ms(args.watch) / 1000,
            idle_ms=duration_ms(args.idle),
            limit=args.limit,
        )
        make(args.project)  # refuse at once when the policy or provider is unusable
        try:
            loop.run()
        except KeyboardInterrupt:
            pass
        return {"status": "stopped", "passes": loop.passes}
    client, conversation, ceiling = make(args.project)
    since = int(time.time() * 1000) - duration_ms(args.since) if args.since else None
    return consolidation.consolidate(
        client,
        conversation,
        project=args.project,
        since_ms=since,
        limit=args.limit,
        max_classification=ceiling,
    )


def render_consolidation(result: dict[str, Any]) -> str:
    status = result["status"]
    if status == "nothing_new":
        return f"Nothing new to consolidate (frontier {result.get('frontier', 0)})."
    if status == "busy":
        return "The project is not idle yet; nothing was sent."
    if status == "not_admitted":
        return (
            f"Dispatched {result['observations']} observation(s) to the local recording provider; "
            "it writes no audit record, so nothing was admitted or filed."
        )
    if status == "stopped":
        return f"Stopped after {result['passes']} pass(es)."
    lines = [
        f"Consolidation {status}: {len(result['filed'])} claim(s) filed for review from "
        f"{result['observations']} observation(s); {result['claims_seen']} drafted."
    ]
    if result["dropped"]:
        lines.append("Dropped, with reasons: " + json.dumps(result["dropped"], sort_keys=True))
    for item in result["filed"]:
        note = (
            f" ({item['relation']['type']} {item['relation']['claim']})"
            if "relation" in item
            else ""
        )
        lines.append(f"  {item['revision']} [{item['kind']}]{note}")
    lines.append("Review them with `memoryking review list`; nothing here is accepted.")
    return "\n".join(lines)


def start_consolidation(args: Any, socket: Path) -> Any:
    """The opt-in schedule inside the daemon: it talks to its own socket as the owner and
    reaches a model only through the egress policy it was started with."""
    import tempfile

    from memoryking import consolidation
    from memoryking.sdk import LocalClient

    if args.egress_policy is None or not args.consolidate_project:
        raise LocalError("egress_policy_required")
    scratch = Path(tempfile.mkdtemp(prefix="mk-cons-"))
    os.chmod(scratch, 0o700)

    def make(project: str) -> tuple[Any, Any, str | None]:
        client = LocalClient(socket, args.control / "owner.credential", project)
        conversation, ceiling = consolidation.build_conversation(
            client,
            args.egress_policy,
            args.consolidate_provider,
            args.consolidate_model,
            scratch / "rec",
        )
        return client, conversation, ceiling

    make(args.consolidate_project[0])  # an unusable provider stops the daemon at start
    loop = consolidation.ConsolidationLoop(
        make,
        list(args.consolidate_project),
        every_s=duration_ms(args.consolidate_every) / 1000,
        idle_ms=duration_ms(args.consolidate_idle),
    )
    loop.start()
    return loop


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


def review_command(args: Any) -> int:
    """`memoryking review ...`: the daemon decides authority; this only asks and prints."""
    from memoryking import reviewing

    reviewer = reviewing.Reviewer(args.project, **reviewing.connection_args(args))
    action = args.review_command
    if action == "list":
        found = [i for i in reviewer.waiting(args.kind) if i["created_position"] > args.after]
        page, more = found[: args.limit], len(found) > args.limit
        page = [reviewer.revision(i["revision"]) for i in page]
        try:
            asked = reviewer.retirements()["items"]
        except LocalError as exc:
            if exc.code != "denied":
                raise
            asked = []
        if args.json:
            report = {
                "proposals": page,
                "more": more,
                "next_after": page[-1]["created_position"] if more else None,
                "retirement_requests": asked,
            }
            sys.stdout.buffer.write(canonical_bytes(report) + b"\n")
            return 0
        for item in page:
            print(reviewing.render(item, full=False))
        if not page:
            print("No proposals are waiting.")
        if more:
            print(f"More are waiting: continue with --after {page[-1]['created_position']}")
        for entry in asked:
            print(
                f"{entry['request']}  retirement requested for {entry['observation']} by "
                f"{entry['requested_by']}: {entry['note']}"
            )
            if entry.get("preview"):
                print(f"    record: {entry['preview']!r}")
        return 0
    if action == "show":
        item = reviewer.revision(args.id)
        if args.json:
            sys.stdout.buffer.write(canonical_bytes(item) + b"\n")
        else:
            print(reviewing.render(item, full=True))
        return 0
    if action == "accept":
        result = reviewer.accept(args.id, args.as_kind, args.reason, args.effect)
        print(json.dumps(result if args.json else {**result["accepted"], "state": "accepted"}))
        return 0
    if action == "reject":
        item = reviewer.revision(args.id, excerpts=False)
        result = reviewer.decide(item, "reject", "rejected_in_cli", args.reason)
        print(json.dumps(result))
        return 0
    decision = "approve" if action == "retire-approve" else "dismiss"
    args_: dict[str, Any] = {
        "request": args.id,
        "decision": decision,
        "reason": "approved_in_cli" if decision == "approve" else "dismissed_in_cli",
    }
    if args.reason:
        args_["note"] = args.reason
    result = reviewer.call("retire_decide", args_, "cli-retire-" + decision + "-" + args.id)
    print(json.dumps(result))
    return 0


def pin_command(args: Any) -> None:
    """`memoryking pin add|remove|list`: a policy decision by an admin or reviewer."""
    from memoryking import reviewing

    reviewer = reviewing.Reviewer(args.project, **reviewing.connection_args(args))
    if args.pin_command == "list":
        report = reviewer.call("pins", {})
        if args.json:
            sys.stdout.buffer.write(canonical_bytes(report) + b"\n")
            return
        for pin in report["items"]:
            print(f"{pin['target_kind']:<12} {pin['target']}  (pinned by {pin['pinned_by']})")
        if not report["items"]:
            print("Nothing is pinned.")
        return
    kind, target = ("claim", args.claim) if args.claim else ("observation", args.observation)
    result = reviewer.call(
        "pin",
        {"mode": args.pin_command, "target_kind": kind, "target": target},
        f"cli-pin-{args.pin_command}-{kind}-{target}-{int(time.time() * 1000)}",
    )
    print(json.dumps(result))


def unreviewed_command(args: Any) -> None:
    """`memoryking unreviewed on|off|show`: the per-project tier policy (an admin decision)."""
    from memoryking import reviewing

    reviewer = reviewing.Reviewer(args.project, **reviewing.connection_args(args))
    if args.unreviewed_command == "show":
        result = reviewer.call("unreviewed_tier_show", {})
    else:
        result = reviewer.call(
            "unreviewed_tier",
            {"mode": args.unreviewed_command},
            f"cli-unreviewed-{args.unreviewed_command}-{int(time.time() * 1000)}",
        )
    if args.json:
        sys.stdout.buffer.write(canonical_bytes(result) + b"\n")
    else:
        print(
            f"Unreviewed drafts in briefings for {args.project}: "
            + ("ON" if result["enabled"] else "off")
        )


def read_credential(path: Path) -> str:
    private_file(path)
    return path.read_text().strip()


def duration(ms: int) -> str:
    """A whole-number span such as '2 d 3 h 4 min 5 s', for operator messages."""
    seconds, parts = max(0, ms) // 1000, []
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= size:
            parts.append(f"{seconds // size} {unit}")
            seconds %= size
    parts.append(f"{seconds} s")
    return " ".join(parts)


def doctor(
    store: Path,
    control: Path,
    socket: Path | None,
    credential: Path | None,
    embedding_model: Path | None = None,
    backup_dir: Path | None = None,
    backup_every: str | None = None,
    *,
    reranker_model: Path | None = None,
) -> list[tuple[str, str, str]]:
    """Health checks an operator can run while the daemon is up. Each is (level, name, detail)."""
    import sqlite3

    from memoryking.adapters.local_files import Control, private_dir
    from memoryking.adapters.token_count import encoding

    findings: list[tuple[str, str, str]] = []

    def record(level: str, name: str, detail: str) -> None:
        findings.append((level, name, detail))

    if sys.version_info[:2] in {(3, 12), (3, 13)}:
        record("ok", "python", sys.version.split()[0])
    else:
        record("fail", "python", f"{sys.version.split()[0]} found; MemoryKing needs 3.12 or 3.13")
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
            lexical_state: list[tuple[str, str, int]] = []
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                lexical_state = lexical_findings(db, version)
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
            for tenant, state, remaining in lexical_state:
                if state == "current":
                    record("ok", "lexical index", f"{tenant}: current (lexical-v2)")
                else:
                    total = lexical_total(database, tenant)
                    done = (
                        f"{(total - remaining) * 100 // total}% rebuilt, about "
                        f"{duration((remaining // RATE_PER_SECOND + 1) * 1000)} left at the reference rate; "
                        if total
                        else ""
                    )
                    record(
                        "warn",
                        "lexical index",
                        f"{tenant}: {state}, still on the previous configuration (lexical-v1) with "
                        f"{remaining} observations to rewrite; {done}"
                        + (
                            "run `memoryking reindex`; "
                            if state == "legacy"
                            else "`memoryking reindex --wait` shows progress; "
                        )
                        + "the daemon finishes it in batches and captures keep working",
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
        ahead_ms = anchor.get("last_utc", 0) - SystemClock().utc_ms()
        if ahead_ms > 0:
            record(
                "fail",
                "clock",
                f"the latest time this store recorded is {duration(ahead_ms)} ahead of the system "
                "clock, so every request fails with clock_untrusted until the clock reaches it "
                "(a clock that jumped forward and was corrected leaves this); see the deployment "
                "guide, Clock recovery",
            )
        else:
            record("ok", "clock", "system clock is at or after the latest recorded time")
    except LocalError as exc:
        record("fail", "journal", f"{exc.code}; stay offline and use the recovery guide")
    except OSError as exc:
        record("fail", "journal", f"{type(exc).__name__} reading {control}")
    try:
        encoding()
        record("ok", "tokenizer", "exact cl100k_base available")
    except LocalError:
        record(
            "warn",
            "tokenizer",
            "exact tokenizer missing; a characters-per-token estimate is used and disclosed",
        )
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
    if reranker_model is not None:
        try:
            from memoryking.adapters.local_embeddings import RERANKER, model_files, model_manifest

            model_files(reranker_model, model_manifest(RERANKER))
            record("ok", "reranker model", f"{reranker_model} verified")
        except (LocalError, OSError) as exc:
            code = exc.code if isinstance(exc, LocalError) else type(exc).__name__
            record(
                "fail",
                "reranker model",
                f"{reranker_model}: {code}; run `python -m memoryking.model_install --model reranker --destination DIR`",
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


def render_health(report: dict[str, Any]) -> str:
    """The memory-health report as readable text: signals first, then the counts."""
    window = report["window"]
    observations, briefings = report["observations"], report["briefings"]
    feedback, claims, backlog = report["feedback"], report["claims"], report["backlog"]

    def ratio(ppm: int | None) -> str:
        return "n/a" if ppm is None else f"{ppm / 10000:.0f}%"

    lines = [f"Memory health for {report['project']} over the last {window['days']} day(s)", ""]
    for item in report["signals"]:
        lines.append(f"[{item['level']}] {item['name']}: {item['text']}")
    lines += [
        "",
        f"Observations: {observations['total']} live, {observations['recent']} captured in the window, {observations['held']} held, {observations['purged']} purged",
        f"  reached a retained delivery: {observations['delivered']}; never delivered: {observations['never_delivered']} ({ratio(observations['never_delivered_ppm'])})",
        f"Briefings: {briefings['total']} retained; no relevant memory: {briefings['no_relevant_memory']} ({ratio(briefings['no_relevant_memory_ppm'])}); deliveries of every kind: {briefings['deliveries']}",
        f"Feedback: {feedback['votes']} vote(s), {feedback['rated']} rated, {feedback['helpful']} helpful ({ratio(feedback['helpful_ppm'])})",
        f"Claims: {claims['families']} families; "
        + ", ".join(f"{count} {state}" for state, count in claims["by_state"].items())
        + f"; {claims['quarantined']} quarantined, {claims['evidence_purged']} lost their evidence, {claims['superseded']} replaced",
        f"Review backlog: {backlog['proposed']} waiting"
        + (
            f", oldest {backlog['oldest_age_ms'] // 86_400_000} day(s)"
            if backlog["oldest_age_ms"] is not None
            else ""
        ),
    ]
    for item in backlog["items"]:
        lines.append(
            f"  - {item['revision']} {item['logical_key']} ({item['kind']} by {item['issuer']}, {item['age_ms'] // 86_400_000} day(s))"
        )
    if backlog["truncated"]:
        lines.append("  - ...")
    drafts = report.get("drafts", {"by_method": {}})
    for method, entry in drafts["by_method"].items():
        lines.append(
            f"Drafts by {method}: {entry['drafted']} drafted, {entry['accepted']} accepted, "
            f"{entry['rejected']} rejected, {entry['waiting']} waiting; accepted of decided "
            f"{ratio(entry['accepted_of_decided_ppm'])}"
        )
    lines += ["", f"Coverage: {report['coverage'].replace('_', ' ')}"]
    return "\n".join(lines) + "\n"


def default_trust_key(project: str, source: str, level: str) -> str:
    """The retry key for a trust rule: readable when it is valid, else a stable hash.

    A glob such as `scraper-*` (or the project `*`) is outside the key alphabet, and a
    long source exceeds its length, so those derive from a digest of the rule instead.
    """
    key = f"trust-{project}-{source}-{level}"
    if re.fullmatch(r"[A-Za-z0-9_-]{1,96}", key):
        return key
    digest = hashlib.sha256(canonical_bytes([project, source, level])).hexdigest()
    return f"trust-{digest[:32]}"


def autostart(args: Any) -> None:
    """Start the profile's daemon when a command needs its socket and the profile allows it."""
    if (
        not args.profile
        or args.command in {"doctor", "stop"}
        or getattr(args, "socket", None) is None
        or getattr(args, "store", None) is not None
    ):
        return
    profile = profiles.resolve(args.profile, args.config)
    if profile.get("autostart") and Path(args.socket) == Path(profile["socket"]):
        daemon.ensure(args.profile, args.config)


def main() -> int:
    if sys.argv[1:2] == ["mcp"]:
        # The bridge has its own options; argparse would claim the ones that start with `--`.
        from memoryking.adapters import stdio

        return stdio.main(sys.argv[2:])
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
    adder.add_argument(
        "--autostart",
        action="store_true",
        help="Let the CLI and the stdio bridge start this store's daemon when it is not running",
    )
    adder.add_argument("--embedding-model", type=Path, help="Installed local model directory")
    adder.add_argument("--reranker-model", type=Path, help="Installed local reranker directory")
    adder.add_argument("--backup-dir", type=Path)
    adder.add_argument("--backup-every", help="Such as 12h or 1d")
    adder.add_argument("--backup-keep", type=int)
    adder.add_argument(
        "--binding", action="append", default=[], metavar="KEY=VALUE", help=BINDING_HELP
    )
    prof_commands.add_parser("list")
    prof_commands.add_parser("remove").add_argument("name")
    for name in ("init", "serve", "backup", "restore", "gc", "control-checkpoint"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--store", type=Path)
        cmd.add_argument("--control", type=Path)
        if name in {"serve", "backup", "gc"}:
            cmd.add_argument(
                "--instance-id",
                help="Name this deployment for the instance lease; set it on every process of a multi-host deployment (default: derived from the control directory)",
            )
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
                "--reranker-model",
                type=Path,
                help="Installed local cross-encoder; briefings that ask for `rerank` have "
                "their top candidates reordered by it (a profile may set it)",
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
                "--workers",
                type=int,
                help="Requests served at once (default 8; 1 to 256)",
            )
            cmd.add_argument(
                "--waiters",
                type=int,
                help="Connections that may wait for a free worker, up to five seconds each "
                "(default 16; 0 to 1024); more are refused as service_busy",
            )
            cmd.add_argument(
                "--health-port",
                type=int,
                help="Loopback port serving /healthz (liveness) and /readyz (readiness) "
                "without credentials; 0 chooses a free port; off by default",
            )
            cmd.add_argument(
                "--summary-idle",
                default="30m",
                help="Derive a 'where this left off' summary for a task idle this long "
                "(e.g. 30m, 2h); 0 disables",
            )
            cmd.add_argument(
                "--consolidate-every",
                help="Off by default. Run background consolidation this often (e.g. 6h) for "
                "each --consolidate-project, when it has been idle; needs --egress-policy. "
                "The daemon process then reads the provider's key file",
            )
            cmd.add_argument(
                "--consolidate-project", action="append", default=[], metavar="PROJECT"
            )
            cmd.add_argument("--consolidate-provider", help="A provider in the egress policy")
            cmd.add_argument("--consolidate-model", help="A model the policy allows for it")
            cmd.add_argument(
                "--consolidate-idle", default="15m", help="Quiet time before a pass (15m)"
            )
        if name == "init":
            cmd.add_argument("--tenant", default="main")
            cmd.add_argument("--principal", default="owner")
        if name in {"backup", "restore"}:
            cmd.add_argument("--backup", type=Path, required=True)
        if name == "backup":
            cmd.add_argument(
                "--incremental-from",
                type=Path,
                help="An earlier backup of this store on the same filesystem: blobs it holds "
                "are hard-linked after their hash is verified, not copied",
            )
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
    stopper = commands.add_parser("stop", help="Stop the background daemon of the profile")
    stopper.add_argument("--store", type=Path)
    stopper.add_argument("--control", type=Path)
    stopper.add_argument("--socket", type=Path)
    setup = commands.add_parser(
        "setup",
        help="Connect a host in one idempotent run: store, daemon, agent credential, MCP, hooks",
    )
    setup.add_argument("host", choices=("claude-code", "codex", "generic"))
    setup.add_argument("--project", required=True, help="The project the agent works in")
    setup.add_argument("--principal", default="agent", help="Agent principal (default agent)")
    setup.add_argument(
        "--data-dir", type=Path, help="Where a new store lives; default ~/.memoryking/NAME"
    )
    setup.add_argument(
        "--scope",
        choices=("user", "project"),
        default="user",
        help="Where the session hooks go: your home directory (default) or this directory",
    )
    setup.add_argument("--cwd", type=Path, help="Project directory for --scope project")
    setup.add_argument("--task", help="Task the hooks resume and checkpoint")
    setup.add_argument("--expires", help="Credential validity such as 90d; default none")
    setup.add_argument(
        "--dashboard-port",
        type=int,
        help="Also serve the loopback dashboard on this port (0 chooses one); needs a daemon restart",
    )
    setup.add_argument("--no-hooks", action="store_true", help="Register the MCP bridge only")
    setup.add_argument("--dry-run", action="store_true", help="Print every change, make none")
    setup.add_argument("--remove", action="store_true", help="Undo this host's registration")
    add_binding(setup)
    mcp = commands.add_parser(
        "mcp",
        help="Run the MCP stdio bridge (arguments as for `python -m memoryking.adapters.stdio`)",
    )
    mcp.add_argument("rest", nargs="*", help="Bridge options; run `memoryking mcp --help`")
    reindex = commands.add_parser(
        "reindex",
        help="Start the rebuild into the current lexical configuration; --wait shows progress",
    )
    reindex.add_argument("--socket", type=Path)
    reindex.add_argument("--credential", type=Path, help="An admin credential file")
    reindex.add_argument("--project", default="main", help="Project used to read the progress")
    reindex.add_argument(
        "--wait", action="store_true", help="Show progress until the rebuild completes"
    )
    reindex.add_argument("--json", action="store_true", help="Print the raw result")
    doc = commands.add_parser("doctor", help="Check the installation and report fixes")
    doc.add_argument("--store", type=Path)
    doc.add_argument("--control", type=Path)
    doc.add_argument("--socket", type=Path, help="Also query the running daemon")
    doc.add_argument("--credential", type=Path, help="Admin credential for the daemon query")
    doc.add_argument("--embedding-model", type=Path, help="Model directory to verify")
    doc.add_argument("--reranker-model", type=Path, help="Reranker directory to verify")
    doc.add_argument("--backup-dir", type=Path, help="Scheduled backup directory to check")
    doc.add_argument("--backup-every", help="Backup policy interval the directory should meet")
    journal = commands.add_parser(
        "journal", help="Inspect the control journal (offline; stop the daemon first)"
    )
    journal_commands = journal.add_subparsers(dest="journal_command", required=True)
    pending = journal_commands.add_parser(
        "pending",
        help="List journal records the database has not applied: sequence, kind, tenant and "
        "the error from the last attempt, never record content",
    )
    pending.add_argument("--store", type=Path)
    pending.add_argument("--control", type=Path)
    cred = commands.add_parser("credential", help="Issue, list and revoke agent credentials")
    cred_commands = cred.add_subparsers(dest="credential_command", required=True)
    rotate_owner = cred_commands.add_parser(
        "rotate-owner",
        help="Offline: replace the owner credential file with a new credential; stop the daemon first",
    )
    rotate_owner.add_argument("--store", type=Path)
    rotate_owner.add_argument("--control", type=Path)
    rotate_owner.add_argument(
        "--instance-id", help="The id the running instance uses, when it sets one"
    )
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
            item.add_argument(
                "--rotate",
                action="store_true",
                help="Replace the principal's credential: every earlier one stops working",
            )
            who = item.add_mutually_exclusive_group()
            who.add_argument(
                "--for-user",
                metavar="USER",
                help="The user this agent acts for, so it reads and writes their own memory "
                "across projects; default: the issuing owner",
            )
            who.add_argument(
                "--no-user",
                action="store_true",
                help="Do not let this credential see anyone's user memory",
            )
            item.add_argument("--output", type=Path, required=True, help="New private file")
        if name == "list":
            item.add_argument("--json", action="store_true")
    usr = commands.add_parser(
        "user", help="Your own memory across projects: show, export or erase it"
    )
    usr_commands = usr.add_subparsers(dest="user_command", required=True)
    for name, text in (
        ("show", "What your user memory holds and which agents act for you"),
        ("export", "Write your user memory as Markdown files"),
        ("purge", "Erase your user memory and everything built from it"),
    ):
        sub = usr_commands.add_parser(name, help=text)
        sub.add_argument("--socket", type=Path)
        sub.add_argument("--credential", type=Path)
        sub.add_argument("--json", action="store_true")
        if name == "export":
            sub.add_argument("--dir", type=Path, required=True, help="Directory to write")
        if name == "purge":
            sub.add_argument(
                "--user", help="Administrators only: purge this user's scope without reading it"
            )
            sub.add_argument("--yes", action="store_true", help="Confirm: this cannot be undone")
    summ = commands.add_parser(
        "summarize", help="Derive a 'where this left off' summary of a task, with its own receipt"
    )
    summ.add_argument("--socket", type=Path)
    summ.add_argument("--credential", type=Path)
    summ.add_argument("--project", help="Default: MEMORYKING_PROJECT")
    summ.add_argument("--task", required=True)
    summ.add_argument("--run")
    summ.add_argument("--egress-policy", type=Path, help="With --provider: the daemon's policy")
    summ.add_argument("--provider", help="Write the summary with this policy adapter")
    summ.add_argument("--model", help="A model the policy allows for that provider")
    summ.add_argument("--json", action="store_true")
    add_binding(summ)
    cons = commands.add_parser(
        "consolidate",
        help="Have a model draft claims from recent observations, for review; never accepts",
    )
    cons.add_argument("--socket", type=Path)
    cons.add_argument("--credential", type=Path, help="Credential with capture and inference")
    cons.add_argument("--project", required=True)
    cons.add_argument("--since", help="Only observations recorded within this span, e.g. 7d")
    cons.add_argument("--limit", type=int, help="Observations per pass, 1 to 64 (24)")
    cons.add_argument("--egress-policy", type=Path, help="The daemon's policy file")
    cons.add_argument("--provider", help="A policy provider, or local-recording to dry-run")
    cons.add_argument("--model", help="A model the policy allows for that provider")
    cons.add_argument(
        "--watch",
        metavar="EVERY",
        help="Keep running: a pass this often (e.g. 6h) while the project is idle",
    )
    cons.add_argument("--idle", default="15m", help="With --watch: quiet time before a pass")
    cons.add_argument("--json", action="store_true")
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
    cap.add_argument("--project", help="Default: MEMORYKING_PROJECT")
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
    cap.add_argument(
        "--scope",
        choices=("project", "user"),
        default="project",
        help="`user` saves a durable fact or preference about you that applies in every "
        "project (never project facts or secrets)",
    )
    cap.add_argument(
        "--supersedes",
        metavar="OBSERVATION",
        help="An earlier observation you can read that this one replaces; it stays as evidence "
        "but briefings prefer this one",
    )
    rec = commands.add_parser("recall", help="Print a cited Markdown briefing for a query")
    rec.add_argument("--socket", type=Path)
    rec.add_argument("--credential", type=Path)
    rec.add_argument("--project", help="Default: MEMORYKING_PROJECT")
    rec.add_argument("--query", help="What you are about to work on (or use --start)")
    rec.add_argument(
        "--start",
        action="store_true",
        help="No query: constraints, pinned items, the task's last checkpoint and recent decisions",
    )
    rec.add_argument("--days", type=int, help="With --start: how far back recent decisions go")
    rec.add_argument("--budget", type=int, default=6000)
    rec.add_argument("--task")
    rec.add_argument(
        "--as-of", help="Brief as the project stood then: a date, ISO date-time, or Unix ms"
    )
    rec.add_argument(
        "--from",
        dest="from_time",
        help="Only records observed at or after this: a date, ISO date-time, or Unix ms",
    )
    rec.add_argument(
        "--to",
        dest="to_time",
        help="Only records observed at or before this (a bare date means the end of the day)",
    )
    rec.add_argument(
        "--salience",
        action="store_true",
        help="Let your own recent use of memories nudge the ranking (bounded, reported)",
    )
    rec.add_argument(
        "--rerank",
        action="store_true",
        help="Let the daemon's local cross-encoder reorder the top candidates (needs serve --reranker-model)",
    )
    rec.add_argument("--json", action="store_true", help="Print the structured packet instead")
    add_binding(rec)
    sp = commands.add_parser(
        "save-progress", help="Save where a task stands (the lean save_progress tool)"
    )
    res = commands.add_parser(
        "resume", help="Print a task's latest saved progress (the lean resume tool)"
    )
    for item in (sp, res):
        item.add_argument("--socket", type=Path)
        item.add_argument("--credential", type=Path)
        item.add_argument("--project", help="Default: MEMORYKING_PROJECT")
        item.add_argument("--task", required=True, help="Task id, stable across sessions")
        item.add_argument("--run")
        item.add_argument("--json", action="store_true", help="Print the structured result")
        add_binding(item)
    sp.add_argument("--objective", required=True)
    sp.add_argument("--state", required=True, help="Where the task stands now")
    sp.add_argument("--next", action="append", metavar="ACTION", help="Repeatable")
    sp.add_argument("--finding", action="append", metavar="TEXT", help="Repeatable")
    sp.add_argument("--blocker", action="append", metavar="TEXT", help="Repeatable")
    sp.add_argument("--constraint", action="append", metavar="TEXT", help="Repeatable")
    sp.add_argument(
        "--failed",
        action="append",
        type=failed_attempt,
        metavar='"ACTION => OUTCOME"',
        help="A failed attempt and why; repeatable",
    )
    sp.add_argument("--share-with", action="append", metavar="PRINCIPAL", help="Repeatable")
    res.add_argument("--budget", type=int, default=12000)
    rv = commands.add_parser(
        "review", help="Review waiting proposals and retirement requests from the terminal"
    )
    rv_commands = rv.add_subparsers(dest="review_command", required=True)
    for name, text in (
        ("list", "List proposals waiting for a decision, oldest first, with their evidence"),
        ("show", "Show one proposal in full"),
        ("accept", "Accept a proposal (knowledge, or --as constraint)"),
        ("reject", "Reject a proposal; --reason is required"),
        ("retire-approve", "Approve a retirement request: purge the record (admin)"),
        ("retire-dismiss", "Dismiss a retirement request; the record stays"),
    ):
        sub = rv_commands.add_parser(name, help=text)
        sub.add_argument("--socket", type=Path)
        sub.add_argument("--credential", type=Path)
        sub.add_argument("--project", required=True)
        sub.add_argument("--json", action="store_true")
        if name == "list":
            sub.add_argument("--limit", type=int, default=10, help="Proposals per page")
            sub.add_argument("--after", type=int, default=0, help="Continue after this position")
            sub.add_argument("--kind", choices=("knowledge", "constraint"), default="knowledge")
        else:
            sub.add_argument("id", help="A revision id, or a retirement request id")
        if name == "accept":
            sub.add_argument("--as", dest="as_kind", choices=("knowledge", "constraint"))
            sub.add_argument(
                "--effect",
                choices=("require", "prohibit"),
                default="require",
                help="With --as constraint: whether the text requires or prohibits",
            )
        if name in {"accept", "reject", "retire-approve", "retire-dismiss"}:
            sub.add_argument(
                "--reason",
                required=name in {"reject", "retire-dismiss"},
                help="Why, in your own words; recorded with the decision",
            )
    pn = commands.add_parser("pin", help="Keep an accepted claim or observation in session start")
    pn_commands = pn.add_subparsers(dest="pin_command", required=True)
    for name in ("add", "remove", "list"):
        sub = pn_commands.add_parser(name)
        sub.add_argument("--socket", type=Path)
        sub.add_argument("--credential", type=Path)
        sub.add_argument("--project", required=True)
        sub.add_argument("--json", action="store_true")
        if name != "list":
            target = sub.add_mutually_exclusive_group(required=True)
            target.add_argument("--claim", metavar="REVISION_OR_FAMILY")
            target.add_argument("--observation", metavar="OBSERVATION")
    un = commands.add_parser(
        "unreviewed",
        help="Let briefings show unreviewed drafts in a labelled section (admin; off by default)",
    )
    un_commands = un.add_subparsers(dest="unreviewed_command", required=True)
    for name in ("on", "off", "show"):
        sub = un_commands.add_parser(name)
        sub.add_argument("--socket", type=Path)
        sub.add_argument("--credential", type=Path)
        sub.add_argument("--project", required=True)
        sub.add_argument("--json", action="store_true")
    dr = commands.add_parser(
        "draft-proposals", help="File claim drafts from repeated evidence into the review queue"
    )
    dr.add_argument("--socket", type=Path)
    dr.add_argument("--credential", type=Path)
    dr.add_argument("--project", required=True)
    dr.add_argument("--limit", type=int, default=16)
    dr.add_argument(
        "--no-procedures",
        action="store_true",
        help="Skip procedure drafts from outcomes that recur across tasks",
    )
    dr.add_argument("--key", help="Retry key; default: one per project and day")
    dr.add_argument("--json", action="store_true")
    vault = commands.add_parser(
        "vault", help="Export memory as a Markdown vault for hand editing, or import one"
    )
    vault_commands = vault.add_subparsers(dest="vault_command", required=True)
    for name, text in (
        ("export", "Write every observation and claim as <project>/<source>/<name>.md"),
        ("import", "Capture changed or new files as versions; claim files become proposals"),
    ):
        sub = vault_commands.add_parser(name, help=text)
        sub.add_argument("--socket", type=Path)
        sub.add_argument("--credential", type=Path)
        sub.add_argument("--project", help="Default: MEMORYKING_PROJECT")
        sub.add_argument(
            "--scope",
            choices=("project", "user"),
            default="project",
            help="`user` is your own memory across projects, as the `user` folder",
        )
        sub.add_argument("--dir", type=Path, required=True, help="The vault directory")
        sub.add_argument("--json", action="store_true")
        if name == "import":
            sub.add_argument(
                "--dry-run", action="store_true", help="Report what would change; write nothing"
            )
    infl = commands.add_parser(
        "influence", help="Where one observation went: every record built from it and who got it"
    )
    infl.add_argument("observation")
    infl.add_argument("--socket", type=Path)
    infl.add_argument("--credential", type=Path)
    infl.add_argument("--project", required=True)
    infl.add_argument("--json", action="store_true", help="Print the structured report instead")
    health = commands.add_parser(
        "health",
        help="Is memory paying for itself: feedback, empty briefings, lost evidence, backlog",
    )
    health.add_argument("--socket", type=Path)
    health.add_argument("--credential", type=Path)
    health.add_argument("--project", required=True)
    health.add_argument("--days", type=int, default=30, help="Trailing window, 1 to 365 (30)")
    health.add_argument("--json", action="store_true", help="Print the structured report instead")
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
    hk.add_argument(
        "--query", help="Briefing query at session start; default: the query-free session start"
    )
    hk.add_argument("--budget", type=int, default=6000)
    add_binding(hk)
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
    add_binding(inst)
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
        if args.command == "setup":
            from memoryking import setup_host

            return setup_host.run(args)
        if args.command == "profile":
            if args.profile_command == "add":
                values: dict[str, Any] = {
                    "store": str(args.store.resolve()),
                    "control": str(args.control.resolve()),
                }
                for field in (
                    "credential",
                    "socket",
                    "embedding_model",
                    "reranker_model",
                    "backup_dir",
                ):
                    if getattr(args, field) is not None:
                        values[field] = str(getattr(args, field).resolve())
                for field in ("dashboard_port", "backup_every", "backup_keep"):
                    if getattr(args, field) is not None:
                        values[field] = getattr(args, field)
                if args.autostart:
                    values["autostart"] = True
                if args.binding:
                    values["bindings"] = session_bindings.parse_pairs(args.binding)
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
        autostart(args)
        optional = (
            {"socket", "credential"}
            if args.command
            in {
                "doctor",
                "capture",
                "recall",
                "hook",
                "hooks",
                "influence",
                "trust",
                "draft-proposals",
                "unreviewed",
                "consolidate",
                "save-progress",
                "resume",
                "review",
                "pin",
            }
            else set()
        )
        if args.command in {"capture", "recall", "summarize"} and args.project is None:
            args.project = os.environ.get("MEMORYKING_PROJECT")
            if not args.project:
                raise LocalError("project_required")
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
        elif args.command == "stop":
            if not args.profile:
                raise LocalError("profile_required")
            result = daemon.stop(profiles.resolve(args.profile, args.config))
            print(json.dumps(result))
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
                supersedes=args.supersedes,
                scope=args.scope,
                socket=args.socket,
                credential=args.credential,
                project=args.project,
            )
            print(json.dumps({k: result[k] for k in ("observation", "position") if k in result}))
        elif args.command == "recall":
            from memoryking import hooks

            if bool(args.query) == bool(args.start):
                raise LocalError("invalid_request")
            where = {
                "socket": args.socket,
                "credential": args.credential,
                "project": args.project,
                "profile": args.profile,
                "config": args.config,
            }
            packet = (
                hooks.start(
                    budget=args.budget,
                    text=not args.json,
                    task=args.task,
                    days=args.days,
                    binding=args.binding,
                    **where,
                )
                if args.start
                else hooks.recall(
                    args.query,
                    budget=args.budget,
                    text=not args.json,
                    task=args.task,
                    as_of=hooks.as_of_ms(args.as_of) if args.as_of else None,
                    time_range={
                        **(
                            {"from_ms": hooks.range_ms(args.from_time, end=False)}
                            if args.from_time
                            else {}
                        ),
                        **(
                            {"to_ms": hooks.range_ms(args.to_time, end=True)}
                            if args.to_time
                            else {}
                        ),
                    }
                    or None,
                    salience=args.salience,
                    rerank=args.rerank,
                    binding=args.binding,
                    **where,
                )
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
                            payload,
                            query=args.query,
                            task=args.task,
                            budget=args.budget,
                            binding=args.binding,
                            **where,
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
                binding=args.binding,
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
                reranker_model=args.reranker_model,
            )
            width = max(len(name) for _, name, _ in findings)
            for level, name, detail in findings:
                print(f"{level.upper():<5} {name.ljust(width)}  {detail}")
            failed = sum(level == "fail" for level, _, _ in findings)
            print(f"{len(findings)} checks, {failed} failed")
            return 1 if failed else 0
        elif args.command == "journal":
            print(json.dumps(LocalStore.pending_journal(args.store, args.control)))
        elif args.command == "credential" and args.credential_command == "rotate-owner":
            path = LocalStore.rotate_owner(args.store, args.control, instance_id=args.instance_id)
            print(json.dumps({"owner_credential": str(path), "rotated": True}))
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
                unknown = sorted(set(grant["capabilities"]) - (CAPABILITIES - {"admin"}))
                if unknown:
                    # Say which name is wrong and which exist, not a bare invalid_request.
                    raise LocalError(
                        "invalid_capabilities",
                        detail={
                            "path": "--capabilities",
                            "problem": "unknown capability " + ", ".join(unknown[:4]),
                            "expected": "any of " + ", ".join(sorted(CAPABILITIES - {"admin"})),
                        },
                    )
                if args.rotate:
                    grant["rotate"] = True
                if not args.no_user and {"capture", "read"} & set(grant["capabilities"]):
                    grant["for_user"] = args.for_user or "@self"
                if args.expires:
                    grant["expires"] = int(time.time() * 1000) + duration_ms(args.expires)
                result = request(args.socket, credential, "grant", grant)
                atomic(args.output, result.pop("credential").encode())
                result.update(credential_file=str(args.output), **grant)
                print(json.dumps(result))
        elif args.command == "user":
            return user_command(args)
        elif args.command in {"save-progress", "resume"}:
            result = lean_command(args)
            if args.json:
                sys.stdout.buffer.write(canonical_bytes(result) + b"\n")
            elif args.command == "resume":
                print(result["text"], end="")
            else:
                print(json.dumps({k: result[k] for k in ("task", "revision", "id") if k in result}))
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
        elif args.command == "consolidate":
            result = consolidate_command(args)
            if args.json:
                sys.stdout.buffer.write(canonical_bytes(result) + b"\n")
            else:
                print(render_consolidation(result))
        elif args.command == "review":
            outcome = review_command(args)
            if outcome:
                return outcome
        elif args.command == "pin":
            pin_command(args)
        elif args.command == "unreviewed":
            unreviewed_command(args)
        elif args.command == "draft-proposals":
            token = read_credential(args.credential)
            context = request(args.socket, token, "context", {"project": args.project})
            day = int(time.time() * 1000) // 86_400_000
            report = request(
                args.socket,
                token,
                "knowledge",
                {
                    "command": {
                        "version": 1,
                        "incarnation": context["incarnation"],
                        "action": "draft",
                        "key": args.key or f"draft-{args.project}-{day}",
                        "args": {
                            "project": args.project,
                            "limit": args.limit,
                            "procedures": not args.no_procedures,
                        },
                    }
                },
            )
            if args.json:
                sys.stdout.buffer.write(canonical_bytes(report) + b"\n")
            else:
                for item in report["drafted"]:
                    print(
                        f"{item['revision']} [{item['basis']} {item['topic']}, {item['support']} sources] {item['text']}"
                    )
                print(
                    f"{len(report['drafted'])} draft(s) filed for review from {report['window']} observations; "
                    f"skipped {json.dumps(report['skipped'], sort_keys=True)}"
                )
                procedures = report.get("procedures")
                if procedures is not None:
                    for item in procedures["drafted"]:
                        print(
                            f"{item['revision']} [{item['polarity']} procedure, {item['tasks']} tasks] {item['text']}"
                        )
                    print(
                        f"{len(procedures['drafted'])} procedure draft(s) filed for review "
                        f"(they still need a review and a separate activation); "
                        f"skipped {json.dumps(procedures['skipped'], sort_keys=True)}"
                    )
        elif args.command == "vault":
            from memoryking import vault as vault_module

            client = vault_module.connect(
                socket=args.socket,
                credential=args.credential,
                project=args.project,
                scope=args.scope,
            )
            folder = "user" if args.scope == "user" else None
            if args.vault_command == "export":
                report = vault_module.export(args.dir, client, folder)
            else:
                report = vault_module.import_(
                    args.dir, client, dry_run=args.dry_run, directory_name=folder
                )
            if args.json:
                sys.stdout.buffer.write(canonical_bytes(report) + b"\n")
            elif args.vault_command == "export":
                print(
                    f"Exported {report['observations']} file(s) from {report['versions']} "
                    f"observation(s) and {report['claims']} claim(s) to {report['directory']}"
                )
            else:
                verb = "Would capture" if report["dry_run"] else "Captured"
                for item in report["captured"]:
                    print(f"{verb} {item['file']} as version {item['version']}")
                for item in report["proposed"]:
                    print(
                        f"{verb.replace('capture', 'propose')} {item['file']} (claim {item['key']})"
                    )
                for item in report["errors"]:
                    print(f"Skipped {item['file']}: {item['error']}", file=sys.stderr)
                print(
                    f"{len(report['captured'])} captured, {len(report['proposed'])} proposed, "
                    f"{report['unchanged']} unchanged, {len(report['errors'])} skipped"
                )
                if report["errors"]:
                    return 1
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
                command["key"] = args.key or default_trust_key(
                    args.project, args.source, args.level
                )
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
        elif args.command == "health":
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
                        "action": "health",
                        "args": {"project": args.project, "days": args.days},
                    }
                },
            )
            if args.json:
                sys.stdout.buffer.write(canonical_bytes(report) + b"\n")
            else:
                print(render_health(report), end="")
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
            if args.profile:
                result["daemon"] = daemon.state(profiles.resolve(args.profile, args.config))
            if args.json:
                sys.stdout.buffer.write(canonical_bytes(result) + b"\n")
            else:
                print(render_status(result, __version__))
        elif args.command == "reindex":
            private_file(args.credential)
            token = args.credential.read_text().strip()
            started = request(args.socket, token, "reindex", {})
            lexical = started.get("lexical", {})
            if not args.json:
                print("Reindex started: " + lexical_line(lexical))
            while args.wait and lexical.get("state") != "current":
                time.sleep(1)
                lexical = request(args.socket, token, "status", {"project": args.project})[
                    "lexical"
                ]
                if not args.json:
                    print("Lexical    " + lexical_line(lexical), flush=True)
            if args.json:
                sys.stdout.buffer.write(canonical_bytes({"lexical": lexical}) + b"\n")
            elif args.wait:
                print("Reindex complete.")
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
            with LocalStore(
                args.store, args.control, instance_id=getattr(args, "instance_id", None)
            ) as store:
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
                    consolidation_loop = None
                    if args.backup_dir is not None:
                        from memoryking.backups import Policy

                        store.backup_policy = Policy(
                            args.backup_dir, args.backup_every or "1d", args.backup_keep or 14
                        )
                    if args.embedding_model is not None:
                        from memoryking.adapters.local_embeddings import LocalOnnxEmbedding
                        from memoryking.embedding_loop import EmbeddingLoop

                        store.embedder = LocalOnnxEmbedding(args.embedding_model)
                    if args.reranker_model is not None:
                        from memoryking.adapters.local_embeddings import LocalOnnxReranker

                        store.reranker = LocalOnnxReranker(args.reranker_model)
                    health_server = None
                    if args.health_port is not None:
                        from memoryking.health import LocalHealthServer

                        if not 0 <= args.health_port <= 65535:
                            raise LocalError("invalid_health_configuration")
                        health_server = LocalHealthServer(store.readiness, args.health_port)
                        event("serve", "health", port=health_server.start())
                    if args.dashboard_port is not None:
                        if not 0 <= args.dashboard_port <= 65535:
                            raise LocalError("invalid_dashboard_configuration")
                        dashboard = Dashboard(store, args.dashboard_port)
                        store.dashboard = dashboard
                        dashboard.start()
                    pid_file = store.root / daemon.PID
                    try:
                        with Server(store, workers=args.workers, waiters=args.waiters) as server:
                            atomic(pid_file, (str(os.getpid()) + "\n").encode())
                            print(
                                json.dumps({"ready": True, "socket": str(server.path)}), flush=True
                            )
                            event("serve", "ready", dashboard=dashboard is not None)
                            if args.consolidate_every:
                                consolidation_loop = start_consolidation(args, server.path)
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
                        pid_file.unlink(missing_ok=True)
                        if consolidation_loop is not None:
                            consolidation_loop.stop()
                        if embeddings is not None:
                            embeddings.stop()
                        if dashboard:
                            dashboard.stop()
                        if health_server:
                            health_server.stop()
                elif args.command == "backup":
                    print(json.dumps(store.backup(args.backup, previous=args.incremental_from)))
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
        code = exc.code if isinstance(exc, LocalError) else "invalid_request"
        extra = dict(exc.detail) if isinstance(exc, LocalError) else {}
        if code == "transport_unavailable" and "start" not in extra:
            extra["start"] = daemon.start_hint(args.profile)
        print(json.dumps({"error": code, "hint": hint(code), **extra}), file=sys.stderr)
        return 2
    except (OSError, ValueError):
        code = "local_io_or_configuration_error"
        print(json.dumps({"error": code, "hint": hint(code)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
