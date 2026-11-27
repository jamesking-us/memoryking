"""A session-end hook that hands its work to a background process, and finishes it later.

Codex stops a session-end hook after 3 seconds, and a capture can take longer than that when
the daemon has to be started first (it loads the search model). So for Codex the hook only
writes a small record of what it was asked to do, starts a detached worker that does it, and
returns at once. The record lives in the profile's private control directory until the work is
done; a worker that is killed, a machine that loses power, or a daemon that cannot be reached
leaves it there, and the next session-start hook starts a worker for every record that is
still waiting. The work is the ordinary `host_hooks.save` call, whose ids are deterministic
(and whose observation time is the one recorded when the hook first ran), so doing it twice,
or at once in two workers, stores it once.

What a record holds: the hook's own arguments and the event the host sent (a session id, a
transcript path and the like), never the transcript. The transcript's tail is read when the
work is done, so a transcript that is deleted before then cannot be captured. A record is given
up after `TRIES` failed attempts, or `KEEP_SECONDS`, so an unreachable store does not make
session starts spawn workers for ever.

A record is an instruction that a later process turns into a command, so it is trusted no
more than it has to be. The directory must be private and not a link; a record is opened
without following a link and must be a regular file of this user, private, and at most
`MAX_RECORD` bytes; its arguments must parse, with the real hook parser, to exactly what
`hand_off` writes (a Codex `session-end` hook on a local store, no relay, no `--spool`, and a
credential in this same control directory); its event must be a string of at most `MAX_EVENT`
characters. A record that fails any of this is removed and no worker is started for it.
Windows has no local store and runs the capture inside the hook.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any

from memoryking.adapters import platform_support
from memoryking.domain.local import LocalError

DIRECTORY = "hook-spool"
TRIES = 5
KEEP_SECONDS = 7 * 86_400
SETTLE_SECONDS = 15  # a record younger than this belongs to a worker that is probably running
REPLAY_AT_MOST = 5  # workers one session start may start
MAX_EVENT = 65_536  # characters of the host's event a record may hold
MAX_RECORD = 262_144  # bytes of a record file
NAME = re.compile(r"^[0-9a-f]{40}\.json$")


def eligible(args: Any) -> bool:
    """Only a Codex session-end hook on a local store (never Windows, which has none), and
    never the worker itself."""
    return (
        platform_support.LOCAL_STORE_SUPPORTED
        and args.kind == "session-end"
        and args.host == "codex"
        and not args.spool
        and not args.relay
        and isinstance(args.credential, Path)
    )


def directory(args: Any) -> Path:
    return Path(args.credential).parent / DIRECTORY


def _worker(argv: list[str], record: Path) -> None:
    import subprocess

    subprocess.Popen(
        [sys.executable, "-m", "memoryking.hook_cli", *argv, "--spool", str(record)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # the host stopping the hook must not stop the worker
        close_fds=True,
    )


def _open_directory(where: Path) -> int:
    """The spool directory as a descriptor, after checking it is private and not a link."""
    from memoryking.adapters.local_files import private_dir

    private_dir(where.parent)
    private_dir(where)
    return platform_support.open_directory_nofollow(where)


def _read(handle: int, name: str) -> dict[str, Any] | None:
    """The record `name` in the directory, or None when it is not a record we may use: a link,
    a special file, one of another user or readable by others, too big, or not JSON."""
    try:
        fd = platform_support.open_regular_nofollow(name, dir_fd=handle)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or not platform_support.owned_by_current_user(info.st_uid)
            or info.st_mode & 0o077
            or info.st_size > MAX_RECORD
        ):
            return None
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(MAX_RECORD + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_RECORD:
        return None
    try:
        body = json.loads(data.decode("utf-8"))
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _parse(argv: Any) -> Any:
    """The arguments as the hook parser reads them, or None when it would refuse them."""
    from memoryking import hook_cli, profiles

    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        return None
    try:
        parsed = hook_cli.build_parser(hook_cli.QuietParser).parse_args(argv)
        profiles.apply(parsed, parsed.profile, parsed.config)
    except (ValueError, SystemExit, LocalError, OSError):
        return None
    return parsed


def valid(body: dict[str, Any], where: Path) -> Any:
    """The parsed arguments of a record that is exactly what `hand_off` writes, else None."""
    parsed = _parse(body.get("argv"))
    event, at, tries = body.get("event"), body.get("at"), body.get("tries")
    if (
        parsed is None
        or not eligible(parsed)
        or not isinstance(event, str)
        or len(event) > MAX_EVENT
        or isinstance(at, bool)
        or not isinstance(at, int)
        or isinstance(tries, bool)
        or not isinstance(tries, int)
        or directory(parsed) != where
    ):
        return None
    return parsed


def hand_off(args: Any, argv: list[str]) -> bool:
    """Write the record and start the worker; False when that was not possible, and the hook
    then does the work itself."""
    from memoryking.adapters.local_files import atomic, private_dir

    try:
        raw = "" if sys.stdin.isatty() else platform_support.read_stdin_text()
        args.stdin_text = raw  # the hook does the work itself if the hand-off fails
        if len(raw) > MAX_EVENT:
            return False
        where = directory(args)
        private_dir(where.parent)
        private_dir(where, create=True)
        name = hashlib.sha256(json.dumps([argv, raw]).encode()).hexdigest()[:40] + ".json"
        record = where / name
        if not record.exists():
            body = {"argv": argv, "event": raw, "at": int(time.time() * 1000), "tries": 0}
            atomic(record, json.dumps(body).encode())
        _worker(argv, record)
    except (OSError, LocalError):
        return False
    return True


def replay(args: Any) -> None:
    """Start a worker for each record a killed hook or worker left behind (called by the
    session-start hook; it does not wait for them). A record that is not valid is removed."""
    if not platform_support.LOCAL_STORE_SUPPORTED or not isinstance(
        getattr(args, "credential", None), Path
    ):
        return
    where = directory(args)
    try:
        handle = _open_directory(where)
    except (OSError, LocalError):
        return  # not private, a link, or not there: leave it alone
    try:
        started = 0
        for name in sorted(os.listdir(handle)):
            if not NAME.match(name) or started >= REPLAY_AT_MOST:
                continue
            try:
                age = time.time() - os.stat(name, dir_fd=handle, follow_symlinks=False).st_mtime
            except OSError:
                continue
            body = _read(handle, name)
            if body is None or valid(body, where) is None:
                try:
                    os.unlink(name, dir_fd=handle)
                except OSError:
                    pass
                continue
            if age < SETTLE_SECONDS:
                continue
            os.utime(name, dir_fd=handle)  # another session start soon after leaves it alone
            _worker(body["argv"], where / name)
            started += 1
    except OSError:
        return
    finally:
        os.close(handle)


def finish(args: Any, record: Path, started_with: list[str]) -> int:
    """The worker: do what the record says. `started_with` is the worker's own command line.
    The record is removed when the capture is stored (or there was nothing to store) and kept,
    with one more try counted, when it could not be. A record that is not valid is removed."""
    from memoryking import host_hooks
    from memoryking.adapters.local_files import atomic

    mine = started_with[:-2] if started_with[-2:-1] == ["--spool"] else None
    try:
        where = directory(args)
        if (
            mine is None
            or not NAME.match(record.name)
            or record.parent.resolve() != where.resolve()
            or args.kind != "session-end"
        ):
            return 1
        handle = _open_directory(where)
    except (OSError, LocalError):
        return 1
    try:
        body = _read(handle, record.name)
        if body is None:
            try:
                os.unlink(record.name, dir_fd=handle)  # not usable; or taken by another worker
            except OSError:
                pass
            return 0
        if body.get("argv") != mine or valid(body, where) is None:
            os.unlink(record.name, dir_fd=handle)
            return 1
    except OSError:
        return 1
    finally:
        os.close(handle)
    if body is None:
        return 1
    event_args = {
        "socket": args.socket,
        "credential": args.credential,
        "project": args.project,
        "profile": args.profile,
        "config": args.config,
    }
    try:
        event = host_hooks.read_event(body["event"])
        host_hooks.save(
            event,
            "session-end",
            host=args.host,
            task=args.task,
            user_preferences=False if args.no_user_preferences else None,
            observed_ms=int(body["at"]),
            **event_args,
        )
    except (LocalError, OSError, ValueError, KeyError) as exc:
        code = exc.code if isinstance(exc, LocalError) else type(exc).__name__
        # A conflict means this very capture is already stored under another day's record.
        if code != "idempotency_conflict":
            body["tries"] = int(body.get("tries", 0)) + 1
            expired = time.time() * 1000 - int(body["at"]) > KEEP_SECONDS * 1000
            if body["tries"] >= TRIES or expired:
                record.unlink(missing_ok=True)
            else:
                atomic(record, json.dumps(body).encode())
            return 1
    record.unlink(missing_ok=True)
    return 0
