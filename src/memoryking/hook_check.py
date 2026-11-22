"""Evidence that the hooks setup wrote work as Claude Code runs them, without a model.

`memoryking doctor --replay-hooks` reads the hook commands from Claude Code's settings file
and runs each one the way Claude Code does on Windows: the command string through Git Bash,
and through PowerShell (which Claude Code uses when Git Bash is absent), with the event JSON
on stdin; hooks in the shell-free form (`command` plus `args`) run with no shell, as Claude
Code runs them. It replays two sessions per shell, in order: SessionStart, UserPromptSubmit (timed
against the prompt hook's deadline), PreCompact and SessionEnd with a transcript holding a
fact no earlier session knew, then a second SessionStart and prompt that must both bring
that fact back. It reports pass or fail per step. Nothing calls a model; the fact and the
transcripts are synthetic, and they go into the project's memory as ordinary hook captures.

The relay and its profile are read from those same commands, so this needs no other file.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

EVENTS = {
    "session-start": "SessionStart",
    "prompt": "UserPromptSubmit",
    "pre-compact": "PreCompact",
    "session-end": "SessionEnd",
}
PROMPT_DEADLINE = 1.5


Command = str | list[str]  # a shell string, or the shell-free form's argv


def commands(settings: Path) -> dict[str, Command]:
    """The MemoryKing hook command for each kind, from a Claude Code settings file: the
    string a shell runs, or, for a hook in the shell-free form, `command` plus `args`."""
    config = json.loads(settings.read_text(encoding="utf-8"))
    found: dict[str, Command] = {}
    for event, entries in config.get("hooks", {}).items():
        for entry in entries:
            for hook in entry.get("hooks", []):
                args = hook.get("args")
                line = str(hook.get("command", ""))
                if isinstance(args, list):
                    line = " ".join([line, *map(str, args)])
                if "memoryking" in line and " hook " in line:
                    kind = line.split(" hook ", 1)[1].split()[0]
                    if EVENTS.get(kind) == event:
                        found[kind] = (
                            [str(hook.get("command", "")), *map(str, args)]
                            if isinstance(args, list)
                            else line
                        )
    return found


def relay_of(command: Command) -> dict[str, str]:
    """The relay options a hook command carries (`--relay`, `--relay-profile`, ...)."""
    words = command.split() if isinstance(command, str) else command
    found = {}
    for flag in ("--relay", "--relay-profile", "--relay-python", "--project"):
        if flag in words and words.index(flag) + 1 < len(words):
            found[flag.lstrip("-")] = words[words.index(flag) + 1]
    return found


def git_bash() -> str | None:
    """The bash Claude Code uses on Windows: CLAUDE_CODE_GIT_BASH_PATH, or Git for Windows'.
    Never the `bash.exe` that starts WSL."""
    named = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    if named and Path(named).is_file():
        return named
    git = shutil.which("git")
    if git:
        for candidate in (
            Path(git).resolve().parent.parent / "bin" / "bash.exe",
            Path(git).resolve().parent.parent.parent / "bin" / "bash.exe",
        ):
            if candidate.is_file():
                return str(candidate)
    default = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"
    return str(default) if default.is_file() else None


def shells() -> dict[str, list[str]]:
    """How each shell runs one command string."""
    found: dict[str, list[str]] = {}
    bash = git_bash()
    if bash:
        found["Git Bash"] = [bash, "-c"]
    power = shutil.which("pwsh") or shutil.which("powershell")
    if power:
        found[Path(power).stem] = [power, "-NoProfile", "-NonInteractive", "-Command"]
    return found


def transcript(path: Path, prompt: str, fact: str) -> None:
    """Lines in the shape Claude Code writes: the prompt, some tool work, a closing message."""
    lines = [
        {"type": "user", "message": {"role": "user", "content": prompt}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I'll check the deploy configuration."},
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "deploy.toml"}],
            },
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": fact}]},
        },
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


def run(shell: list[str], command: Command, event: dict[str, Any]) -> tuple[int, str, str, float]:
    """One hook: a string through `shell`, or the shell-free form's argv with no shell."""
    started = time.monotonic()
    done = subprocess.run(
        [*shell, command] if isinstance(command, str) else command,
        input=json.dumps(event).encode(),
        capture_output=True,
        timeout=120,
        check=False,
    )
    seconds = time.monotonic() - started
    return (
        done.returncode,
        done.stdout.decode("utf-8", errors="replace"),
        done.stderr.decode("utf-8", errors="replace"),
        seconds,
    )


def replay(settings: Path) -> tuple[bool, list[str]]:
    """Two replayed sessions per shell; (all passed, report lines)."""
    found = commands(settings)
    missing = [kind for kind in EVENTS if kind not in found]
    lines = [f"hooks in {settings}: " + ", ".join(sorted(found))]
    if missing:
        return False, [*lines, "FAIL: no MemoryKing hook for " + ", ".join(missing)]
    # Claude Code runs the shell-free form with no shell, so it is replayed once that way.
    shell_free = all(isinstance(command, list) for command in found.values())
    available = {"no shell (command plus args)": []} if shell_free else shells()
    if not available:
        return False, [*lines, "FAIL: neither Git Bash nor PowerShell was found"]
    ok = True
    with tempfile.TemporaryDirectory(prefix="mk-replay-") as tmp:
        for name, shell in available.items():
            tag = uuid.uuid4().hex[:8]
            fact = f"The release-{tag} deploy window opens on Tuesdays at 14:00 UTC, set by ops."
            ask = f"When does the release-{tag} deploy window open?"
            path = Path(tmp) / f"{tag}.jsonl"
            transcript(path, ask, fact)
            first, second = f"replay-{tag}-1", f"replay-{tag}-2"
            base = {"cwd": str(Path.cwd()), "transcript_path": str(path)}
            steps: list[tuple[str, str, dict[str, Any], str | None]] = [
                ("SessionStart", "session-start", {"session_id": first, "source": "startup"}, None),
                ("UserPromptSubmit", "prompt", {"session_id": first, "prompt": ask}, None),
                ("PreCompact", "pre-compact", {"session_id": first, "trigger": "manual"}, None),
                ("SessionEnd", "session-end", {"session_id": first, "reason": "other"}, None),
                (
                    "second SessionStart",
                    "session-start",
                    {"session_id": second, "source": "startup"},
                    f"release-{tag}",
                ),
                (
                    "second UserPromptSubmit",
                    "prompt",
                    # Other wording than the first prompt (the prompt hook leaves out a record
                    # holding the prompt verbatim), in words the fact holds (its relevance
                    # floor wants most of the prompt's terms matched).
                    {
                        "session_id": second,
                        "prompt": f"Is the release-{tag} deploy window on Tuesdays?",
                    },
                    f"release-{tag}",
                ),
            ]
            for label, kind, fields, expect in steps:
                event = {**base, **fields, "hook_event_name": EVENTS[kind]}
                code, out, err, seconds = run(shell, found[kind], event)
                passed = code == 0 and (expect is None or expect in out)
                note = f"{seconds * 1000:.0f} ms"
                if kind == "prompt":
                    within = seconds <= PROMPT_DEADLINE + 1.0
                    note += f" (deadline {PROMPT_DEADLINE} s for the lookup)"
                    passed = passed and within
                if expect is not None:
                    note += (
                        ", carried the first session's fact" if expect in out else ", fact missing"
                    )
                if not passed and err.strip():
                    note += " | " + err.strip().splitlines()[-1][:160]
                ok = ok and passed
                lines.append(f"{'pass' if passed else 'FAIL'} {name}: {label}: {note}")
    return ok, lines


def settings_files(args: Any) -> list[Path]:
    """Claude Code settings that may hold MemoryKing hooks: the one named, else user and project."""
    if getattr(args, "settings", None):
        return [Path(args.settings)]
    found = [Path.home() / ".claude" / "settings.json", Path.cwd() / ".claude" / "settings.json"]
    return [path for path in found if path.is_file()]


def doctor(args: Any) -> int:
    """`memoryking doctor` where the store is behind a relay (Windows), or `--replay-hooks`."""
    from memoryking import __version__
    from memoryking.domain.local import LocalError
    from memoryking.relay_client import connect

    configured = [(path, commands(path)) for path in settings_files(args)]
    configured = [(path, found) for path, found in configured if found]
    if args.replay_hooks:
        if not configured:
            print("FAIL: no MemoryKing hooks found; run `memoryking setup claude-code` first")
            return 1
        ok, lines = replay(configured[0][0])
        print("\n".join(lines))
        print("hooks replay: " + ("pass" if ok else "FAIL"))
        return 0 if ok else 1
    if not configured:
        print("MemoryKing is not set up for Claude Code here.")
        print("next step: memoryking setup claude-code --project NAME")
        return 1
    path, found = configured[0]
    where = relay_of(next(iter(found.values())))
    spec, profile = where.get("relay"), where.get("relay-profile")
    print(f"hooks: {', '.join(sorted(found))} in {path}")
    if not spec or not profile:
        print("next step: run `memoryking setup claude-code` again; these hooks name no relay")
        return 1
    print(f"back end: {spec}, store-side profile {profile}, project {where.get('project', '?')}")
    try:
        endpoint, _ = connect(spec, None, None, where.get("relay-python"), profile)
    except LocalError as exc:
        detail = f" ({dict(exc.detail)})" if exc.detail else ""
        print(f"store side: not reachable: {exc.code}{detail}")
        nexts = {
            "relay_version_mismatch": "install the same MemoryKing version on both sides "
            "(`memoryking setup claude-code` again, with --wheel or --image for a local build)",
            "relay_unavailable": f"start the back end ({spec.split(':')[0]}) and check that "
            "`memoryking setup claude-code` completed",
            "transport_unavailable": "the store-side profile does not start its daemon; run "
            "`memoryking setup claude-code` again",
        }
        print("next step: " + nexts.get(exc.code, "run `memoryking setup claude-code` again"))
        return 1
    endpoint.close()
    print(
        f"store side: reachable through the relay; daemon running; version {__version__} on both sides"
    )
    kind, _, name = spec.partition(":")
    from memoryking.setup_windows import on_store_side, quiet

    owner = profile.removesuffix("-agent")  # the daemon query needs the owner credential
    done = quiet(on_store_side(kind, name, "-m", "memoryking", "--profile", owner, "doctor"))
    report = done.stdout.decode("utf-8", errors="replace").strip()
    print(
        "store-side checks:"
        + ("\n  " + report.replace("\n", "\n  ") if report else " none reported")
    )
    if done.returncode != 0:
        print("next step: fix the store-side finding above")
        return 1
    print("everything checked is in order")
    return 0
