"""`memoryking setup <host> --project P`: connect an agent host in one idempotent run.

One run creates the store and a named profile when none exists, starts the daemon (and
lets it start on demand afterwards), issues one agent credential with the capabilities the
lean tools need, registers the lean MCP bridge where the host's configuration is a file
this repository documents, and installs the session hooks under the same principal. For a
host whose format is not documented here the exact command is printed instead of written.

Every file this command edits is remembered (its original bytes and what was written), so
`--remove` restores it byte for byte, or deletes it when setup created it. Nothing is
written with `--dry-run`.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from memoryking import daemon, host_hooks, profiles, session_bindings
from memoryking.adapters.local_files import atomic, private_dir
from memoryking.adapters.local_ipc import request
from memoryking.adapters.local_store import LocalStore
from memoryking.domain.local import LocalError

# What the lean tools need, and why. `review` is not here: it is only for chat review.
NEEDED = {
    "capture": "remember, correct, propose, forget and feedback write memory",
    "read": "recall, resume, fetch and recent read it",
    "checkpoint": "save_progress writes task progress",
    "handoff": "hand_off passes a task to another principal",
}
HOOK_HOST = {"claude-code": "claude", "codex": "codex"}
CODEX_BEGIN = (
    "# >>> memoryking setup codex: managed block; undo with `memoryking setup codex --remove`"
)
CODEX_END = "# <<< memoryking setup codex"
DURATIONS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def launcher() -> list[str]:
    """`memoryking` when that name on PATH is this installation, else this Python explicitly."""
    mine = Path(sys.executable).parent / "memoryking"
    found = shutil.which("memoryking")
    if found and mine.exists() and Path(found).resolve() == mine.resolve():
        return ["memoryking"]
    return [sys.executable, "-m", "memoryking"]


def mcp_entry(command: list[str]) -> dict[str, Any]:
    """The bridge as a Claude Code `mcpServers` entry. `alwaysLoad` keeps the memory tools in
    every prompt: the host otherwise lists an MCP server's tools by name only and loads a
    schema when the model searches for it (an option of the CLI's own MCP configuration)."""
    return {
        "type": "stdio",
        "command": command[0],
        "args": command[1:],
        "alwaysLoad": True,
    }


def bridge_command(agent_profile: str, project: str, bindings: list[str]) -> list[str]:
    command = [
        *launcher(),
        "mcp",
        "--store-profile",
        agent_profile,
        "--project",
        project,
        "--profile",
        "lean",
    ]
    for pair in bindings:
        command += ["--binding", pair]
    return command


class Files:
    """Edits to files outside the store, with the record that makes them reversible."""

    def __init__(self, state_path: Path, dry: bool):
        self.state_path, self.dry = state_path, dry
        self.state: dict[str, Any] = {"files": {}}
        self.pending: list[tuple[Path, str]] = []  # what a dry run would write
        if state_path.exists():
            self.state = json.loads(state_path.read_text(encoding="utf-8"))

    def write(self, path: Path, text: str) -> str:
        """Write `text` to `path`; returns created, changed or unchanged."""
        current = path.read_bytes() if path.exists() else None
        data = text.encode()
        if current == data:
            return "unchanged"
        record = self.state["files"].setdefault(str(path), {})
        if "original" not in record:
            record["original"] = None if current is None else base64.b64encode(current).decode()
            record["made_dirs"] = []
            parent = path.parent
            while not parent.exists():
                record["made_dirs"].append(str(parent))
                parent = parent.parent
        record["written"] = sha(data)
        if self.dry:
            self.pending.append((path, text))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return "created" if current is None else "changed"

    def save(self) -> None:
        if self.dry or not self.state["files"]:
            return
        private_dir(self.state_path.parent, create=True)
        atomic(self.state_path, json.dumps(self.state, indent=2).encode())

    def save_removal(self) -> None:
        if self.dry:
            return
        if self.state["files"]:
            atomic(self.state_path, json.dumps(self.state, indent=2).encode())
        else:
            self.state_path.unlink(missing_ok=True)

    def restore(self, path: Path, semantic: Any) -> str:
        """Put the file back exactly as it was; if it changed since, remove only our entries."""
        record = self.state["files"].pop(str(path), None)
        if not path.exists():
            return "absent"
        current = path.read_bytes()
        if record is not None and sha(current) == record["written"]:
            original = record["original"]
            if not self.dry:
                if original is None:
                    path.unlink()
                    for directory in record["made_dirs"]:
                        try:
                            Path(directory).rmdir()
                        except OSError:
                            pass
                else:
                    path.write_bytes(base64.b64decode(original))
            return "restored" if original is not None else "deleted"
        updated = semantic(current.decode())
        if updated is None or updated == current.decode():
            return "unchanged"
        if not self.dry:
            path.write_text(updated, encoding="utf-8")
        return "edited"


# --- Codex: a marked block in ~/.codex/config.toml ----------------------------------


def codex_block(command: list[str]) -> str:
    return (
        f"{CODEX_BEGIN}\n[mcp_servers.memoryking]\ncommand = {json.dumps(command[0])}\n"
        f"args = [{', '.join(json.dumps(part) for part in command[1:])}]\n{CODEX_END}\n"
    )


def codex_without(text: str) -> str:
    start = text.find(CODEX_BEGIN)
    if start < 0:
        return text
    end = text.find(CODEX_END, start)
    if end < 0:
        raise LocalError("hook_config_invalid")
    end = text.find("\n", end)
    end = len(text) if end < 0 else end + 1
    before = text[:start]
    if before.endswith("\n\n"):
        before = before[:-1]  # the blank line we added ahead of the block
    return before + text[end:]


def codex_with(text: str, block: str) -> str:
    text = codex_without(text)
    if "[mcp_servers.memoryking]" in text:
        raise LocalError("host_config_conflict")
    if text and not text.endswith("\n"):
        text += "\n"
    return text + ("\n" if text else "") + block


# --- Steps --------------------------------------------------------------------------


def ensure_store(
    name: str, args: Any, home: Path, config: Path, say: dict[str, str]
) -> tuple[dict[str, Any], bool]:
    """The profile's resolved settings, creating store and profile when `name` is new."""
    known = profiles.load(config)
    if name in known:
        profile = profiles.resolve(name, config)
        wanted = {**known[name], "autostart": True}
        if args.dashboard_port is not None:
            wanted["dashboard_port"] = args.dashboard_port
        say["store"] = f"existing profile {name} at {profile['store']}"
        if wanted != known[name]:
            if not args.dry_run:
                profiles.save(name, wanted, config)
            say["store"] = f"existing profile {name}, updated (autostart, dashboard port)"
            profile.update(wanted)
        return profile, False
    base = (args.data_dir or home / ".memoryking" / name).resolve()
    store, control = base / "store", base / "control"
    socket = store / "service.sock"
    if len(os.fsencode(socket)) > 100:
        raise LocalError("socket_path_too_long")
    values: dict[str, Any] = {"store": str(store), "control": str(control), "autostart": True}
    if args.dashboard_port is not None:
        values["dashboard_port"] = args.dashboard_port
    if not args.dry_run:
        LocalStore.initialize(store, control)
        profiles.save(name, values, config)
    say["store"] = f"created at {store} (profile {name}; control data in {control})"
    return {
        **values,
        "socket": str(socket),
        "credential": str(control / "owner.credential"),
    }, True


def refuse_foreign_filesystem(path: Path) -> None:
    """A store on a Windows drive, NTFS or a network share would lose owner-only modes and
    rename durability; refuse it before anything is created."""
    from memoryking.adapters.platform_support import FOREIGN_FILESYSTEMS, filesystem_type

    kind = filesystem_type(path)
    if kind in FOREIGN_FILESYSTEMS:
        raise LocalError("store_filesystem_unsupported", detail={"path": str(path)})


def duration_ms(text: str) -> int:
    if len(text) < 2 or text[-1] not in DURATIONS or not text[:-1].isdigit():
        raise LocalError("invalid_duration")
    return int(text[:-1]) * DURATIONS[text[-1]]


def credential_usable(profile: dict[str, Any], path: Path, project: str) -> bool:
    """The agent credential exists, works for the project and holds every needed capability."""
    try:
        context = request(
            Path(profile["socket"]),
            path.read_text(encoding="utf-8").strip(),
            "context",
            {"project": project},
        )
    except (LocalError, OSError):
        return False
    return set(NEEDED) <= set(context.get("capabilities", []))


def ensure_credential(
    profile: dict[str, Any], args: Any, path: Path, dry: bool, say: dict[str, str]
) -> None:
    if path.exists() and not dry and credential_usable(profile, path, args.project):
        say["credential"] = f"{path.name} already valid for project {args.project}"
        return
    if dry:
        say["credential"] = f"would issue {path.name}: {', '.join(NEEDED)}"
        return
    grant: dict[str, Any] = {
        "principal": args.principal,
        "project": args.project,
        "capabilities": sorted(NEEDED),
    }
    # Link the agent to the issuing owner's user memory, so it can use the user scope.
    grant["for_user"] = "@self"
    if args.expires:
        grant["expires"] = int(time.time() * 1000) + duration_ms(args.expires)
    owner = Path(profile["credential"]).read_text(encoding="utf-8").strip()
    # Restating a grant returns the principal's current credential, so a second project (or
    # a lost file) never invalidates another host's credential; rotation stays explicit.
    token = request(Path(profile["socket"]), owner, "grant", grant)["credential"]
    atomic(path, token.encode())
    say["credential"] = (
        f"issued {path.name} ({', '.join(NEEDED)}; "
        + (f"expires in {args.expires}" if args.expires else "no expiry")
        + ")"
    )


def hook_changes(args: Any, agent_profile: str, home: Path, cwd: Path) -> tuple[Path, str]:
    result = host_hooks.install(
        host=HOOK_HOST[args.host],
        scope=args.scope,
        project=args.project,
        cwd=cwd,
        home=home,
        profile=agent_profile,
        task=args.task,
        binding=args.binding,
        write=False,
        launcher=launcher_parts(),
        tools="mcp",
        prompts=not args.no_prompt_hook,
    )
    return Path(result["path"]), json.dumps(result["config"], indent=2) + "\n"


def launcher_parts() -> list[str]:
    """The launcher as shell words for a hook command (the Python path quoted)."""
    parts = launcher()
    return parts if parts == ["memoryking"] else [json.dumps(parts[0]), *parts[1:]]


def run(args: Any) -> int:
    """Entry point of the `setup` command; prints a JSON result and a short summary."""
    if args.config or os.environ.get("MEMORYKING_CONFIG"):
        raise LocalError("setup_config_unsupported")
    home = Path.home()
    config = profiles.config_path()
    name = args.profile or "default"
    agent_profile = name + "-agent"
    cwd = (args.cwd or Path.cwd()).resolve()
    session_bindings.parse_pairs(args.binding)  # refuse a bad binding before changing anything
    files = Files(config.parent / "setup" / f"{args.host}.json", args.dry_run)
    if args.remove:
        return remove(args, files, home, cwd)
    say: dict[str, str] = {}
    store_only = getattr(args, "store_only", False)
    if store_only:
        refuse_foreign_filesystem(args.data_dir or home / ".memoryking" / name)
    profile, created = ensure_store(name, args, home, config, say)
    control = Path(profile["control"])
    credential = control / f"{args.principal}.credential"
    if not args.dry_run:
        known = profiles.load(config)
        wanted = {
            "store": profile["store"],
            "control": profile["control"],
            "credential": str(credential),
            "autostart": True,
        }
        if known.get(agent_profile) != wanted:
            profiles.save(agent_profile, wanted, config)
    store_line = say["store"]
    if args.dry_run:
        say["store"] = store_line + ("; would start the daemon" if created else "")
    else:
        started = daemon.ensure(name, config)
        say["store"] = (
            store_line
            + "; daemon "
            + (
                f"started in the background (log {Path(profile['store']) / daemon.LOG}), "
                "and it starts on demand from now on"
                if started["started"]
                else "already running"
            )
        )
    ensure_credential(profile, args, credential, args.dry_run, say)
    if store_only:
        # The store side of a Windows setup: the host is configured on the Windows side,
        # which reaches this store through `memoryking relay --profile AGENT_PROFILE`.
        print(json.dumps({"agent_profile": agent_profile, "store": profile["store"], "say": say}))
        return 0
    command = bridge_command(agent_profile, args.project, args.binding)
    say["mcp"], printed = register(args, files, home, command)
    if not HOOK_HOST.get(args.host):
        say["hooks"] = "not installed (no verified hook format for this host)"
    elif args.no_hooks:
        say["hooks"] = "skipped (--no-hooks)"
    else:
        path, text = hook_changes(args, agent_profile, home, cwd)
        say["hooks"] = f"{files.write(path, text)} {path}, run as the same principal as the bridge"
        say["hooks"] += (
            "; the prompt-time recall hook is not installed (--no-prompt-hook)"
            if args.no_prompt_hook
            else "; the prompt-time recall hook recalls for each prompt (turn it off with "
            "--no-prompt-hook, or MEMORYKING_PROMPT_HOOK=off in the host's environment)"
        )
    if args.dry_run or args.no_semantic:
        semantic_step(name, args, profile, say)
    else:
        try:
            semantic_step(name, args, profile, say)
        except LocalError as exc:  # the rest of setup stands; semantic search can be retried
            say["semantic"] = f"not enabled ({exc.code}); retry with `memoryking semantic enable`"
    files.save()
    summary(args, say, printed, files)
    return 0


def register(args: Any, files: Files, home: Path, command: list[str]) -> tuple[str, list[str]]:
    """Register the bridge with the host; returns (summary line, lines for the user to run)."""
    if args.host == "codex":
        path = home / ".codex" / "config.toml"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        try:
            outcome = files.write(path, codex_with(text, codex_block(command)))
        except LocalError as exc:
            if exc.code != "host_config_conflict":
                raise
            return "not written: [mcp_servers.memoryking] already exists in " + str(path), [
                "Set its command and args to:",
                "  " + shlex.join(command),
            ]
        return f"{outcome} {path} (lean profile)", []
    if args.host == "claude-code":
        add = ["claude", "mcp", "add-json", "memoryking", json.dumps(mcp_entry(command))]
        manual = "run the command below (MemoryKing does not edit Claude Code's MCP file)"
        if not getattr(args, "register", False):
            return manual, [shlex.join(add)]
        if args.dry_run:
            return "would run: " + shlex.join(add), []
        found = shutil.which("claude")
        if found is None:
            return "`claude` is not on your PATH; " + manual, [shlex.join(add)]
        # Claude Code's own command registers it, so its file format is never edited here.
        try:
            done = subprocess.run(
                [found, *add[1:]], capture_output=True, text=True, timeout=60, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return "`claude mcp add-json` could not run; " + manual, [shlex.join(add)]
        if done.returncode == 0:
            return (
                "registered with `claude mcp add-json` (undo: `claude mcp remove memoryking`)",
                [],
            )
        reason = (done.stderr or done.stdout).strip().splitlines()
        return (
            "`claude mcp add-json` refused"
            + (f" ({reason[0][:120]})" if reason else "")
            + "; "
            + manual,
            [shlex.join(add)],
        )
    return "printed below, for any MCP host", [
        "command: " + shlex.join(command),
        "JSON: "
        + json.dumps({"mcpServers": {"memoryking": {"command": command[0], "args": command[1:]}}}),
    ]


def remove(args: Any, files: Files, home: Path, cwd: Path) -> int:
    done: list[str] = []
    if args.host == "codex":
        path = home / ".codex" / "config.toml"
        done.append(f"MCP bridge {files.restore(path, codex_without)}: {path}")
    if HOOK_HOST.get(args.host):
        host = HOOK_HOST[args.host]
        path = host_hooks.config_path(host, args.scope, cwd, home)

        def semantic(text: str) -> str:
            entries = host_hooks._entries(host, dict.fromkeys(host_hooks.KINDS, ""))
            merged = host_hooks.merge(json.loads(text), entries, remove=True)
            return json.dumps(merged, indent=2) + "\n"

        done.append(f"hooks {files.restore(path, semantic)}: {path}")
    if args.host == "claude-code":
        done.append("run `claude mcp remove memoryking` to unregister the bridge")
    files.save_removal()
    prefix = "[dry run] " if args.dry_run else ""
    print("\n".join(prefix + line for line in done))
    print(
        "The store, the agent credential and the daemon are kept: `memoryking credential "
        "revoke` withdraws the credential, `memoryking stop` stops the daemon."
    )
    return 0


def semantic_step(name: str, args: Any, profile: dict[str, Any], say: dict[str, str]) -> None:
    """Turn semantic search on when the optional runtime is installed; say what it did.

    This is the one place `setup` may fetch anything: the pinned model, with what is fetched
    and from where printed first. Without the runtime nothing is downloaded and the summary
    says how to get there. `--no-semantic` (or MEMORYKING_SEMANTIC=off) skips all of it."""
    from memoryking import model_install, semantic
    from memoryking.__main__ import semantic_configure

    if args.no_semantic or os.environ.get("MEMORYKING_SEMANTIC") == "off":
        say["semantic"] = "skipped (--no-semantic)"
        return
    problem = semantic.runtime_problem()
    if problem is not None:
        say["semantic"] = (
            f"off, search stays lexical: {problem}. "
            "Install it, then run `memoryking semantic enable`"
        )
        return
    if args.dry_run:
        say["semantic"] = "would install and enable the local model. " + semantic.consent()
        return
    if not semantic.verified():
        print(semantic.consent(), flush=True)
        model_install.install(semantic.model_dir(), semantic.MODEL)
    socket = Path(profile["socket"])
    token = (Path(profile["control"]) / "owner.credential").read_text(encoding="utf-8").strip()
    if (
        request(socket, token, "status", {"project": args.project}).get("semantic", {}).get("model")
        != semantic.MODEL
    ):
        daemon.stop(profile)  # the daemon loads an installed model when it starts
        daemon.ensure(name)
    semantic_configure(argparse.Namespace(socket=socket), token, args.project, "experimental")
    say["semantic"] = (
        f"on for project {args.project} with {semantic.MODEL}; indexing runs in the background "
        "and `memoryking status` shows its progress"
    )


def summary(args: Any, say: dict[str, str], printed: list[str], files: Files) -> None:
    prefix = "[dry run] " if args.dry_run else ""
    lines = [
        f"{prefix}store and daemon: {say['store']}",
        f"{prefix}agent credential: {say['credential']}",
        f"{prefix}MCP bridge: {say['mcp']}",
        f"{prefix}session hooks: {say['hooks']}",
        f"{prefix}semantic search: {say['semantic']}",
        f"undo: memoryking setup {args.host} --project {args.project} --remove",
    ]
    for path, text in files.pending:
        printed = [*printed, f"--- {path} would contain:", text.rstrip("\n")]
    print("\n".join(lines + [""] * bool(printed) + printed))
