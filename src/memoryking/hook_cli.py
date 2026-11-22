"""`memoryking hook`, without the rest of the CLI.

A host runs a hook command on every session start, prompt, compaction and session end,
so its start-up is paid each time. `python -m memoryking.hook_cli ARGS` accepts exactly
the arguments of `memoryking ARGS` for the `hook` command (the global options, then
`hook KIND ...`) and runs the same code, but imports only what a hook needs: not the
store, the dashboard or the other commands. `memoryking hook ...` still works and runs
this module's `run`.

The global options defined here (`--profile`, `--config`, `--relay`, `--relay-python`,
`--binding`) and the relay and autostart steps are shared with `memoryking.__main__`.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import sys
from pathlib import Path
from typing import Any

from memoryking import profiles
from memoryking.adapters import platform_support
from memoryking.domain.local import LocalError
from memoryking.relay_client import connect

BINDING_HELP = (
    "Where this session works, such as environment=prod; repeatable. Also "
    "MEMORYKING_BINDINGS and the named profile's bindings"
)
KINDS = ("session-start", "pre-compact", "session-end", "prompt")


def light(launcher: list[str]) -> list[str]:
    """The same launcher, pointed at this module: `-m memoryking` becomes
    `-m memoryking.hook_cli`, the `memoryking` command `memoryking-hook`; anything else as is."""
    if launcher == ["memoryking"]:
        return ["memoryking-hook"]
    if launcher[-2:] == ["-m", "memoryking"]:
        return [*launcher[:-1], "memoryking.hook_cli"]
    return list(launcher)


def add_binding(parser: Any) -> None:
    parser.add_argument(
        "--binding", action="append", default=[], metavar="KEY=VALUE", help=BINDING_HELP
    )


def add_global_arguments(parser: argparse.ArgumentParser) -> None:
    """The options that come before the command, shared with `memoryking`."""
    parser.add_argument(
        "--profile",
        default=os.environ.get("MEMORYKING_PROFILE"),
        help="Named store from the profiles config; fills omitted --store, --control, "
        "--socket and --credential (MEMORYKING_PROFILE)",
    )
    parser.add_argument(
        "--config", type=Path, help="Profiles file; default ~/.memoryking/config.toml"
    )
    parser.add_argument(
        "--relay",
        default=os.environ.get("MEMORYKING_RELAY"),
        help="Reach the daemon through a relay on the store's side: wsl:DISTRO or "
        "docker:CONTAINER; --socket and --credential are then paths on that side "
        "(MEMORYKING_RELAY)",
    )
    parser.add_argument(
        "--relay-python",
        default=os.environ.get("MEMORYKING_RELAY_PYTHON"),
        help="Python with MemoryKing on the store's side; default the one `memoryking setup` "
        "installs (WSL) or the image's python (container) (MEMORYKING_RELAY_PYTHON)",
    )
    parser.add_argument(
        "--relay-profile",
        default=os.environ.get("MEMORYKING_RELAY_PROFILE"),
        help="Named profile on the store's side that gives the relay its socket and "
        "credential and starts the daemon on demand; no path then crosses to this side "
        "(MEMORYKING_RELAY_PROFILE)",
    )


def add_hook_arguments(hk: argparse.ArgumentParser) -> None:
    hk.add_argument("kind", choices=KINDS)
    hk.add_argument("--host", choices=("claude", "codex", "grok"), help="Host that is calling")
    hk.add_argument("--socket", type=Path)
    hk.add_argument("--credential", type=Path)
    hk.add_argument("--project", help="Default: MEMORYKING_PROJECT")
    hk.add_argument("--task", help="Resume this task at start; checkpoint it before loss")
    hk.add_argument(
        "--query", help="Briefing query at session start; default: the query-free session start"
    )
    hk.add_argument(
        "--budget",
        type=int,
        help="Briefing token budget; default 6000 at session start, 1200 for the prompt hook",
    )
    hk.add_argument("--limit", type=int, help="prompt hook: items at most (default 5, at most 32)")
    hk.add_argument(
        "--floor",
        type=int,
        help="prompt hook: lowest lexical score, in millionths, an item may have (default 300000)",
    )
    hk.add_argument(
        "--deadline",
        type=float,
        help="prompt hook: seconds before the lookup is abandoned and nothing is added (default 1.5)",
    )
    hk.add_argument(
        "--tools",
        choices=("mcp", "cli"),
        help="How the agent reaches memory; the session-start note then names the tools to "
        "save with (hooks installed by `memoryking setup` pass mcp)",
    )
    add_binding(hk)
    hk.add_argument(
        "--strict", action="store_true", help="Exit non-zero on error instead of only logging"
    )


def attach_relay(args: argparse.Namespace) -> None:
    """Send this command's requests through a relay; the token stays on the store's side."""
    if not hasattr(args, "socket"):
        raise LocalError("relay_invalid")
    endpoint, credential = connect(
        args.relay, args.socket, args.credential, args.relay_python, args.relay_profile
    )
    atexit.register(endpoint.close)
    args.socket = endpoint
    if hasattr(args, "credential"):
        args.credential = credential


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
        from memoryking import daemon

        daemon.ensure(args.profile, args.config)


def run(args: argparse.Namespace) -> int:
    """The `hook` command once its connection is resolved; never fails unless --strict."""
    from memoryking import host_hooks

    try:
        raw = "" if sys.stdin.isatty() else platform_support.read_stdin_text()
        payload = host_hooks.read_event(raw)
        where = {
            "socket": args.socket,
            "credential": args.credential,
            "project": args.project,
            "profile": args.profile,
            "config": args.config,
        }
        if args.kind == "prompt":
            chosen = {
                key: value
                for key, value in (
                    ("budget", args.budget),
                    ("limit", args.limit),
                    ("floor", args.floor),
                    ("deadline", args.deadline),
                )
                if value is not None
            }
            sys.stdout.write(host_hooks.prompt(payload, binding=args.binding, **chosen, **where))
        elif args.kind == "session-start":
            sys.stdout.write(
                host_hooks.session_start(
                    payload,
                    query=args.query,
                    task=args.task,
                    budget=args.budget or 6000,
                    binding=args.binding,
                    tools=args.tools,
                    host=args.host,
                    **where,
                )
            )
        else:
            summary = host_hooks.save(payload, args.kind, host=args.host, task=args.task, **where)
            print(json.dumps(summary), file=sys.stderr)
    except (LocalError, OSError, ValueError, KeyError) as exc:
        code = exc.code if isinstance(exc, LocalError) else type(exc).__name__
        print(f"memoryking hook {args.kind}: {code}", file=sys.stderr)
        if args.strict:
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    platform_support.utf8_stdio()
    parser = argparse.ArgumentParser(prog="memoryking", description=__doc__.split("\n")[0])
    add_global_arguments(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    add_hook_arguments(
        commands.add_parser(
            "hook", help="Run as a host hook: read the host's event JSON on stdin, never fail"
        )
    )
    args = parser.parse_args(argv)
    try:
        profiles.apply(args, args.profile, args.config)
        if args.relay:
            attach_relay(args)
        else:
            autostart(args)
    except LocalError as exc:
        # Exactly what `memoryking hook` reports when the connection cannot be resolved.
        from memoryking import daemon
        from memoryking.domain.hints import hint

        extra = dict(exc.detail)
        if exc.code == "transport_unavailable" and "start" not in extra:
            extra["start"] = daemon.start_hint(args.profile)
        print(json.dumps({"error": exc.code, "hint": hint(exc.code), **extra}), file=sys.stderr)
        return 2
    except (OSError, ValueError):
        from memoryking.domain.hints import hint

        code = "local_io_or_configuration_error"
        print(json.dumps({"error": code, "hint": hint(code)}), file=sys.stderr)
        return 2
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
