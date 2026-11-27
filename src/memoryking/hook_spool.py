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
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from memoryking.domain.local import LocalError

DIRECTORY = "hook-spool"
TRIES = 5
KEEP_SECONDS = 7 * 86_400
SETTLE_SECONDS = 15  # a record younger than this belongs to a worker that is probably running
REPLAY_AT_MOST = 5  # workers one session start may start
ENV_OFF = "MEMORYKING_HOOK_HANDOFF"  # set to off to run the capture inside the hook as before


def eligible(args: Any) -> bool:
    """Only a Codex session-end hook on a local store, and never the worker itself."""
    return (
        args.kind == "session-end"
        and args.host == "codex"
        and not args.spool
        and not args.relay
        and isinstance(args.credential, Path)
        and os.environ.get(ENV_OFF, "").lower() != "off"
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


def hand_off(args: Any, argv: list[str]) -> bool:
    """Write the record and start the worker; False when that was not possible, and the hook
    then does the work itself."""
    from memoryking.adapters import platform_support
    from memoryking.adapters.local_files import atomic, private_dir

    try:
        raw = "" if sys.stdin.isatty() else platform_support.read_stdin_text()
        args.stdin_text = raw  # the hook does the work itself if the hand-off fails
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
    session-start hook; it does not wait for them)."""
    try:
        where = directory(args)
        started = 0
        for record in sorted(where.glob("*.json")):
            if started >= REPLAY_AT_MOST:
                break
            if time.time() - record.stat().st_mtime < SETTLE_SECONDS:
                continue
            try:
                body = json.loads(record.read_text(encoding="utf-8"))
                argv = body["argv"]
                if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
                    raise ValueError
            except (OSError, ValueError, KeyError, TypeError):
                record.unlink(missing_ok=True)
                continue
            os.utime(record)  # another session start soon after leaves it to this worker
            _worker(argv, record)
            started += 1
    except OSError:
        return


def finish(args: Any, record: Path) -> int:
    """The worker: do what the record says. The record is removed when the capture is stored
    (or there was nothing to store) and kept, with one more try counted, when it could not be."""
    from memoryking import host_hooks

    try:
        body = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0  # taken by another worker, or not ours
    where = {
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
            **where,
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
                from memoryking.adapters.local_files import atomic

                atomic(record, json.dumps(body).encode())
            return 1
    record.unlink(missing_ok=True)
    return 0
