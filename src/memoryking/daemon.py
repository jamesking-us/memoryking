"""Start, find and stop the local daemon in the background.

A profile with `autostart = true` lets the stdio bridge and the CLI start the daemon when
nothing listens on its socket. Starting is serialised by a lock file in the store
directory, so two starters never race; the daemon logs to `daemon.log` there and writes
`daemon.pid` while it runs.
"""

from __future__ import annotations

import fcntl
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from memoryking import profiles
from memoryking.domain.local import LocalError

LOG = "daemon.log"
PID = "daemon.pid"
LOCK = "daemon.lock"
LOG_LIMIT = 1_048_576
START_SECONDS = 20.0
STOP_SECONDS = 15.0


def alive(socket_path: Path) -> bool:
    """True when something accepts connections on the socket."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(2)
        try:
            probe.connect(str(socket_path))
        except OSError:
            return False
    return True


def read_pid(store: Path) -> int | None:
    try:
        value = int((store / PID).read_text().strip())
    except (OSError, ValueError):
        return None
    return value if value > 1 else None


def _ours(pid: int) -> bool:
    """The process is alive and is a memoryking process (guards against a reused pid)."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        command = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "memoryking" in command


def state(profile: dict[str, Any]) -> dict[str, Any]:
    """What `status` reports about the daemon, from the profile and the filesystem."""
    store = Path(profile["store"])
    pid = read_pid(store)
    running = alive(Path(profile["socket"]))
    return {
        "running": running,
        "pid": pid if running and pid is not None and _ours(pid) else None,
        "autostart": bool(profile.get("autostart")),
        "log": str(store / LOG),
    }


def start_command(name: str | None, config: Path | None) -> list[str]:
    command = [sys.executable, "-m", "memoryking"]
    if config is not None:
        command += ["--config", str(config)]
    if name:
        command += ["--profile", name]
    return command + ["serve", "--log-level", "info"]


def ensure(name: str, config: Path | None = None) -> dict[str, Any]:
    """Make sure the daemon of profile `name` is listening; start it when the profile allows.

    Raises `transport_unavailable` when it is down and the profile does not allow starting."""
    profile = profiles.resolve(name, config)
    path = Path(profile["socket"])
    if alive(path):
        return {"started": False, "socket": str(path)}
    if not profile.get("autostart"):
        raise LocalError("transport_unavailable", detail={"start": start_hint(name)})
    return start(name, profile, config)


def start_hint(name: str | None) -> str:
    return f"memoryking --profile {name} serve" if name else "memoryking serve"


def start(name: str, profile: dict[str, Any], config: Path | None = None) -> dict[str, Any]:
    store = Path(profile["store"])
    path = Path(profile["socket"])
    if len(os.fsencode(path)) > 100:
        raise LocalError("socket_path_too_long")
    lock = os.open(store / LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + START_SECONDS
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if alive(path):  # another starter finished while we waited
                    return {"started": False, "socket": str(path)}
                if time.monotonic() > deadline:
                    raise LocalError("daemon_start_failed") from None
                time.sleep(0.1)
        if alive(path):
            return {"started": False, "socket": str(path)}
        log = store / LOG
        if log.exists() and log.stat().st_size > LOG_LIMIT:
            log.write_bytes(b"")
        out = os.open(log, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            process = subprocess.Popen(
                start_command(name, config),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            os.close(out)
        deadline = time.monotonic() + START_SECONDS
        while time.monotonic() < deadline:
            if alive(path):
                return {"started": True, "socket": str(path), "log": str(log)}
            if process.poll() is not None:
                break
            time.sleep(0.05)
        raise LocalError("daemon_start_failed", detail={"log": str(log)})
    finally:
        os.close(lock)  # releases the lock


def stop(profile: dict[str, Any]) -> dict[str, Any]:
    """Ask the daemon to shut down cleanly (SIGTERM) and wait for its socket to go."""
    store, path = Path(profile["store"]), Path(profile["socket"])
    if not alive(path):
        return {"running": False, "stopped": False}
    pid = read_pid(store)
    if pid is None or not _ours(pid):
        raise LocalError("daemon_stop_failed")
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + STOP_SECONDS
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return {"running": False, "stopped": True}
        time.sleep(0.05)
    raise LocalError("daemon_stop_failed")
