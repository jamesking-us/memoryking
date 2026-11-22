"""`memoryking setup claude-code` on Windows: the store in WSL2 or a container, the host here.

Windows runs the agent-side tools but not the store. One run:

1. finds the back ends this machine has (WSL2 distros, a Docker engine with Linux
   containers), uses the one there is, and stops with the choices when there are several
   (`--backend`), or with what to install when there are none; it never installs either;
2. installs this same MemoryKing version on the store side (WSL2: a venv in the distro's
   own home; container: the image given with `--image`, after checking its version);
3. runs the ordinary setup there in `--store-only` mode, which creates the store under the
   existing owner-only rules (refusing a Windows drive or other foreign file system),
   starts the daemon, issues the agent's limited credential and names its profile;
4. writes Claude Code's hooks and MCP bridge here, reaching the store through
   `memoryking relay --profile AGENT_PROFILE`: no store-side path, credential or secret is
   written on this side, and the hook strings read the same in Git Bash and PowerShell;
5. captures one marked observation through the relay and recalls it, and prints one line
   saying it works or exactly what failed.

`--dry-run` prints every change and makes none; a second run changes nothing it does not
need to; `--remove` puts Claude Code's files back byte for byte from the undo record in
`%LOCALAPPDATA%\\MemoryKing\\setup` (or, where a file changed since, takes out only this
setup's entries) and keeps the store, which it says how to remove.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from memoryking import __version__, host_hooks, setup_host
from memoryking.adapters import platform_support
from memoryking.domain.local import LocalError
from memoryking.relay_client import NAME, WSL_PYTHON

CONTAINER = "memoryking"
# Named volumes only: a bind mount of a Windows folder would put the store on NTFS.
VOLUMES = (
    ("memoryking-data", "/data"),
    ("memoryking-control", "/control"),
    ("memoryking-home", "/home/memoryking"),
)
CONTAINER_DATA = "/data/memoryking"
WINDOWS_HELP = {
    "wsl": "wsl --install  (https://learn.microsoft.com/windows/wsl/install)",
    "docker": "Docker Desktop  (https://docs.docker.com/desktop/setup/install/windows-install/)",
}
WORD = re.compile(r'"[^"]*"|\S+')

# Run inside the distro by `sh -c`, with arguments (want, source, tokenizers): make the
# venv setup's relay uses, with this exact version, and say what was done as one JSON line.
WSL_INSTALL = """set -u
want="$1"; source="$2"; tok="$3"
dir="$HOME/.local/share/memoryking"; py="$dir/venv/bin/python"
case "$HOME" in /mnt/*) echo '{"error":"store_filesystem_unsupported"}'; exit 3;; esac
same() { [ -x "$py" ] && "$py" -c 'import sys, memoryking; sys.exit(memoryking.__version__ != sys.argv[1])' "$want" 2>/dev/null; }
if same && { [ "$tok" = 0 ] || "$py" -c 'import tiktoken' 2>/dev/null; }; then
  echo '{"installed":"already"}'; exit 0
fi
base=""
for c in python3.13 python3.12 "$HOME/.local/bin/python3.13" "$HOME/.local/bin/python3.12" python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(not (3, 12) <= sys.version_info[:2] < (3, 14))' 2>/dev/null; then
    base="$c"; break
  fi
done
[ -n "$base" ] || { echo '{"error":"store_python_missing"}'; exit 4; }
mkdir -p "$dir" && chmod 700 "$dir"
[ -x "$py" ] || "$base" -m venv "$dir/venv" >/dev/null 2>&1 || { echo '{"error":"store_python_missing"}'; exit 4; }
if [ "$source" = index ]; then spec="memoryking==$want"; else spec="$(wslpath -a "$source")"; fi
pip() { "$py" -m pip install -q --disable-pip-version-check "$@" >/dev/null 2>>"$dir/install.log" || { echo '{"error":"store_install_failed"}'; exit 5; }; }
: > "$dir/install.log"
# Every dependency from the hash locks this version ships, then MemoryKing alone.
pip --require-hashes -r "$(wslpath -a "$4")"
if [ "$tok" = 1 ]; then pip --require-hashes -r "$(wslpath -a "$5")"; fi
pip --no-deps "$spec"
same || { echo '{"error":"store_version_mismatch"}'; exit 6; }
echo '{"installed":"now"}'
"""


def quiet(argv: list[str], timeout: float = 600) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(argv, 127, b"", b"")


def wsl_distros() -> list[tuple[str, int]]:
    """(name, WSL version) of every installed distro; `wsl -l -v` prints UTF-16."""
    done = quiet(["wsl.exe", "-l", "-v"], timeout=60)
    if done.returncode != 0:
        return []
    raw = done.stdout
    text = raw.decode("utf-16-le", errors="replace") if b"\x00" in raw else raw.decode()
    found = []
    for line in text.replace("﻿", "").splitlines()[1:]:
        parts = line.replace("*", " ").split()
        if len(parts) >= 3 and parts[-1].isdigit() and NAME.fullmatch(parts[0]):
            found.append((parts[0], int(parts[-1])))
    return found


def docker_engine() -> bool:
    """A running Docker engine with Linux containers."""
    done = quiet(["docker", "info", "--format", "{{.OSType}}"], timeout=60)
    return done.returncode == 0 and done.stdout.decode().strip() == "linux"


def choose(wanted: str | None) -> tuple[str, str, list[str]]:
    """(kind, name, lines saying what was found). Never guesses between candidates."""
    distros = wsl_distros()
    wsl2 = [name for name, version in distros if version == 2]
    engine = docker_engine()
    seen = [f"WSL2 distro {name}" for name in wsl2]
    seen += [f"WSL1 distro {name} (not usable: WSL1)" for name, v in distros if v != 2]
    seen += ["Docker engine with Linux containers"] if engine else []
    found = [f"wsl:{name}" for name in wsl2] + ([f"docker:{CONTAINER}"] if engine else [])
    if wanted:
        kind, _, name = wanted.partition(":")
        if kind == "wsl":
            if not name and len(wsl2) == 1:
                name = wsl2[0]
            if not name:
                raise LocalError(
                    "store_backend_ambiguous" if wsl2 else "store_backend_missing",
                    detail={"found": found, "install": WINDOWS_HELP},
                )
            if name not in wsl2:
                raise LocalError("store_backend_missing", detail={"found": found})
        elif kind == "docker":
            name = name or CONTAINER
            if not engine:
                raise LocalError("store_backend_missing", detail={"found": found})
        else:
            raise LocalError("relay_invalid")
        if not NAME.fullmatch(name):
            raise LocalError("relay_invalid")
        return kind, name, seen
    if not found:
        raise LocalError("store_backend_missing", detail={"install": WINDOWS_HELP})
    if len(found) > 1:
        raise LocalError("store_backend_ambiguous", detail={"found": found})
    kind, _, name = found[0].partition(":")
    return kind, name, seen


def on_store_side(kind: str, name: str, *args: str) -> list[str]:
    """MemoryKing's Python on the store side, run with `args`, never through a shell here."""
    if kind == "docker":
        return ["docker", "exec", "-i", name, "python", *args]
    return ["wsl.exe", "-d", name, "--exec", "/bin/sh", "-c", f'exec "{WSL_PYTHON}" "$@"', "sh"] + [
        *args
    ]


def last_json(done: subprocess.CompletedProcess[bytes]) -> dict[str, Any]:
    for line in reversed(done.stdout.decode("utf-8", errors="replace").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    error = done.stderr.decode("utf-8", errors="replace").strip().splitlines()
    for line in reversed(error):
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return {"error": "store_install_failed"}


def wheel_version(path: Path) -> str:
    parts = path.name.split("-")
    if len(parts) < 3 or parts[0] != "memoryking" or not path.name.endswith(".whl"):
        raise LocalError("store_install_failed", detail={"path": str(path)})
    return parts[1]


def install(kind: str, name: str, args: Any, say: dict[str, str]) -> None:
    """The same MemoryKing version on the store side, with the tokenizers extra when this
    side has it (otherwise briefings would ask for exact counts the store cannot make)."""
    tokenizers = importlib.util.find_spec("tiktoken") is not None
    if kind == "wsl":
        source = "index"
        if args.wheel is not None:
            if wheel_version(args.wheel) != __version__:
                raise LocalError(
                    "store_version_mismatch",
                    detail={"expected": __version__, "found": wheel_version(args.wheel)},
                )
            source = str(args.wheel.resolve())
        what = (
            f"MemoryKing {__version__} in {name} at {WSL_PYTHON.rsplit('/bin/', 1)[0]}"
            + (" with the tokenizers extra" if tokenizers else "")
            + (f" from {args.wheel.name}" if args.wheel else " from the package index")
            + ", its dependencies hash-checked from the locks this version ships"
        )
        if args.dry_run:
            say["install"] = "would install " + what
            return
        locks = Path(__file__).with_name("locks")
        done = quiet(
            ["wsl.exe", "-d", name, "--exec", "/bin/sh", "-c", WSL_INSTALL, "sh"]
            + [__version__, source, "1" if tokenizers else "0"]
            + [str(locks / "core.lock"), str(locks / "tokenizers.lock")]
        )
        result = last_json(done)
        if "error" in result:
            raise LocalError(str(result["error"]), detail={"backend": f"wsl:{name}"})
        if result.get("installed") == "already":
            say["install"] = "already installed: " + what
            return
        say["install"] = what
        # A daemon started before this install still runs the code it loaded, even from a
        # venv that is gone: stop it, and the store step starts it again on this version.
        profile = args.profile or "default"
        stopped = last_json(
            quiet(on_store_side(kind, name, "-m", "memoryking", "--profile", profile, "stop"))
        )
        if stopped.get("stopped") is True:
            say["install"] += "; stopped the daemon that ran the earlier install"
        return
    image = args.image
    if not image:
        raise LocalError("store_image_required")
    version = quiet(["docker", "run", "--rm", image, "--version"], timeout=300)
    found = version.stdout.decode(errors="replace").strip().removeprefix("memoryking ").strip()
    if version.returncode != 0 or found != __version__:
        raise LocalError(
            "store_version_mismatch", detail={"expected": __version__, "found": found or "none"}
        )
    if tokenizers:
        probe = quiet(
            ["docker", "run", "--rm", "--entrypoint", "python", image, "-c", "import tiktoken"]
        )
        if probe.returncode != 0:
            raise LocalError(
                "store_extras_mismatch", detail={"missing_on": "store", "extra": "tokenizers"}
            )
    existing = quiet(["docker", "inspect", "--format", "{{.Config.Image}}", name], timeout=60)
    if existing.returncode == 0:
        current = existing.stdout.decode().strip()
        if current != image:
            raise LocalError("store_version_mismatch", detail={"expected": image, "found": current})
        say["install"] = f"container {name} already runs {image}"
        return
    create = ["docker", "run", "-d", "--name", name, "--init", "--restart", "unless-stopped"]
    for volume, target in VOLUMES:
        create += ["-v", f"{volume}:{target}"]
    create += ["--entrypoint", "sleep", image, "infinity"]
    what = f"container {name} from {image}, named volumes {', '.join(v for v, _ in VOLUMES)}"
    if args.dry_run:
        say["install"] = "would run " + " ".join(create)
        return
    if quiet(create, timeout=300).returncode != 0:
        raise LocalError("store_install_failed", detail={"backend": f"docker:{name}"})
    say["install"] = "started " + what + " (restarts with Docker Desktop)"


def store(kind: str, name: str, args: Any, say: dict[str, str]) -> str:
    """Run the ordinary setup on the store side in --store-only mode; the agent profile."""
    command = ["-m", "memoryking"]
    if args.profile:
        command += ["--profile", args.profile]
    command += ["setup", "claude-code", "--project", args.project, "--store-only"]
    command += ["--principal", args.principal]
    if kind == "docker":
        command += ["--data-dir", CONTAINER_DATA]
    elif args.data_dir is not None:
        command += ["--data-dir", Path(args.data_dir).as_posix()]
    if args.expires:
        command += ["--expires", args.expires]
    if args.no_semantic:
        command += ["--no-semantic"]
    if args.dry_run:
        say["store"] = (
            f"would create or reuse the store on the store side ({kind}:{name}), start its "
            "daemon and issue the agent credential"
        )
        say["credential"] = "issued on the store side; it never leaves it except through the relay"
        return (args.profile or "default") + "-agent"
    result = last_json(quiet(on_store_side(kind, name, *command)))
    if "error" in result:
        raise LocalError(str(result["error"]), detail={"backend": f"{kind}:{name}"})
    said = result.get("say", {})
    say["store"] = f"{said.get('store', 'ready')} ({kind}:{name})"
    say["credential"] = said.get("credential", "issued on the store side")
    say["semantic"] = said.get(
        "semantic", "not changed (turn it on there with `memoryking semantic enable`)"
    )
    return str(result["agent_profile"])


# The first Claude Code seen on Windows to run a hook's `command` with its `args` and no
# shell (2.1.101 ran it as a bash string and lost the backslashes). Below it, or when the
# version cannot be read, setup writes the string form, which both shells read the same.
EXEC_FORM_SINCE = (2, 1, 330)


def claude_version() -> tuple[int, int, int] | None:
    import shutil

    program = shutil.which("claude")
    if program is None:
        return None
    done = quiet([program, "--version"], timeout=60)
    found = re.match(r"\s*(\d+)\.(\d+)\.(\d+)", done.stdout.decode(errors="replace"))
    if done.returncode != 0 or found is None:
        return None
    major, minor, patch = (int(part) for part in found.groups())
    return major, minor, patch


def exec_form(config: dict[str, Any], python: str) -> None:
    """Rewrite MemoryKing's hook commands as `command` (this interpreter) plus `args`, which
    Claude Code runs with no shell at all: nothing is quoted, expanded or rewritten."""
    for entries in config.get("hooks", {}).values():
        for entry in entries:
            for hook in entry.get("hooks", []):
                line = host_hooks.command_line(hook)
                if host_hooks.MARK not in line or " hook " not in line:
                    continue
                words = [json.loads(w) if w.startswith('"') else w for w in WORD.findall(line)]
                hook["command"], hook["args"] = python, words[1:]


def neutral(config: dict[str, Any]) -> None:
    """Every word of every hook command reads the same in Git Bash and PowerShell."""
    for entries in config.get("hooks", {}).values():
        for entry in entries:
            for hook in entry.get("hooks", []):
                for word in WORD.findall(str(hook.get("command", ""))):
                    if not platform_support.neutral_word(word):
                        raise LocalError("hook_command_unsafe", detail={"problem": word[:80]})


def record_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "MemoryKing" / "setup" / "claude-code.json"


class WindowsRecord(setup_host.Files):
    """The byte-exact undo record of the files setup edited, kept in `%LOCALAPPDATA%`.

    It is used only by `--remove` to put those files back; nothing reads it to connect or
    to decide what to trust (commands carry their relay in their own arguments). It holds
    no secret: the original bytes of Claude Code's settings files and what was written."""

    def __init__(self, dry: bool):
        super().__init__(record_path(), dry)

    def save(self) -> None:
        if self.dry or not self.state["files"]:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=2) + "\n", encoding="utf-8")

    def save_removal(self) -> None:
        if self.dry:
            return
        if self.state["files"]:
            self.save()
        else:
            self.state_path.unlink(missing_ok=True)


def check(spec: str, agent: str, project: str) -> str:
    """Capture one marked observation through the relay and recall it."""
    from memoryking.relay_client import connect
    from memoryking.sdk import LocalClient

    marker = f"MemoryKing setup check {time.strftime('%Y-%m-%d %H:%M:%S')} through {spec}"
    endpoint, credential = connect(spec, None, None, None, agent)
    with endpoint:
        client = LocalClient(endpoint, credential, project)
        client.remember(marker, source="setup")
        found = marker in client.recall(marker).text
    if not found:
        raise LocalError("setup_check_failed", detail={"backend": spec})
    return f"MemoryKing works: {spec}, project {project}, version {__version__} on both sides"


def run(args: Any) -> int:
    if args.host != "claude-code":
        raise LocalError("host_unsupported")
    home = Path.home()
    cwd = (args.cwd or Path.cwd()).resolve()
    files = WindowsRecord(args.dry_run)
    if args.remove:
        code = setup_host.remove(args, files, home, cwd)
        print(
            "The store side is kept as well. To remove it: in WSL2, `memoryking --profile "
            "default stop` there and delete ~/.memoryking and ~/.local/share/memoryking; "
            f"with Docker, `docker rm -f {CONTAINER}` and `docker volume rm "
            + " ".join(v for v, _ in VOLUMES)
            + "` (this deletes the memory)."
        )
        return code
    kind, name, seen = choose(args.backend)
    spec = f"{kind}:{name}"
    say: dict[str, str] = {"semantic": "not changed on the store side"}
    print("found: " + ("; ".join(seen) if seen else "nothing") + f"; using {spec}")
    install(kind, name, args, say)
    agent = store(kind, name, args, say)
    python = platform_support.shell_program_path(sys.executable)
    relay = ["--relay", spec, "--relay-profile", agent]
    command = [sys.executable, "-m", "memoryking", "mcp", *relay, "--project", args.project]
    command += ["--profile", "lean"]
    for pair in args.binding:
        command += ["--binding", pair]
    say["mcp"], printed = setup_host.register(args, files, home, command)
    if printed:
        # Windows PowerShell 5.1 drops the inner quotes of a JSON argument; print the command
        # as Command Prompt reads it (also what `--register` runs, with no shell at all).
        add = ["claude", "mcp", "add-json", "memoryking", json.dumps(setup_host.mcp_entry(command))]
        printed = [
            "In Command Prompt (or run setup again with --register to have it done):",
            subprocess.list2cmdline(add),
        ]
    if args.no_hooks:
        say["hooks"] = "skipped (--no-hooks)"
    else:
        result = host_hooks.install(
            host="claude",
            scope=args.scope,
            project=args.project,
            cwd=cwd,
            home=home,
            profile=None,
            task=args.task,
            binding=args.binding,
            write=False,
            launcher=[python, "-m", "memoryking.hook_cli", *relay],
            tools="mcp",
            prompts=not args.no_prompt_hook,
        )
        neutral(result["config"])
        version = claude_version()
        shown = ".".join(map(str, version)) if version else None
        if version is not None and version >= EXEC_FORM_SINCE:
            exec_form(result["config"], sys.executable)
            form = f"shell-free form (command plus args), which Claude Code {shown} runs directly"
        else:
            form = "string form, which Git Bash and PowerShell read the same" + (
                f" (Claude Code {shown} predates the shell-free form)"
                if shown
                else " (Claude Code's version could not be read)"
            )
        path = Path(result["path"])
        text = json.dumps(result["config"], indent=2) + "\n"
        say["hooks"] = f"{files.write(path, text)} {path}; {form}"
    files.save()
    setup_host.summary(args, {**say, "store": f"{say['install']}; {say['store']}"}, printed, files)
    if not args.dry_run:
        print(check(spec, agent, args.project))
    return 0
