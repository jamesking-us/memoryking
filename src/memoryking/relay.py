"""Carry request lines between a pipe and the local daemon's socket, unchanged.

`memoryking relay` (or `python -m memoryking.relay`, which starts faster) runs beside the
daemon, on the machine that holds the store, for a client that cannot reach the Unix
socket itself: a Windows client starts it through `wsl.exe` or `docker exec -i`, so the
pipe is the only channel and there is no network listener and no new secret.

It writes one handshake line, `{"relay": 1, "version": ...}`, plus `credential` (the
token read from `--credential`, after the same owner and mode check the client makes)
when a credential file is named, so the token stays in a file on this side and reaches
the other side only through the pipe. It then reads one request line at a time, opens a
new connection to the daemon for it exactly as a local client does, and writes back the
daemon's one reply line. It never parses or changes a request or a reply, so framing,
the size limit and `service_busy` reach the client as the daemon sent them. A request
line longer than the daemon accepts is forwarded only up to the daemon's limit, which the
daemon refuses as it would on the socket; the rest of that line is discarded.
"""

from __future__ import annotations

import argparse
import json
import socket
import stat
import sys
from pathlib import Path
from typing import BinaryIO

from memoryking import __version__
from memoryking.adapters.platform_support import (
    owned_by_current_user,
    require_local_store,
    unix_stream_socket,
)
from memoryking.domain.hints import hint
from memoryking.domain.local import LocalError

PROTOCOL = 1
# The daemon reads at most this much of a request line (local_ipc.WIRE_LIMIT) and a
# client at most RESPONSE_LIMIT of a reply (local_ipc.request); tests keep them equal.
REQUEST_LIMIT = 262144
RESPONSE_LIMIT = 1048577
SOCKET_SECONDS = 10.0


def _line(value: dict[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _refusal(code: str) -> bytes:
    return _line({"error": code, "hint": hint(code)})


def check_socket(path: Path) -> None:
    """The daemon's own check: the path is a socket owned by this user."""
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or not owned_by_current_user(info.st_uid):
        raise LocalError("unsafe_socket")


def handshake(credential: Path | None) -> bytes:
    body: dict[str, object] = {"relay": PROTOCOL, "version": __version__}
    if credential is not None:
        from memoryking.adapters.local_files import private_file

        private_file(credential)
        body["credential"] = credential.read_text(encoding="utf-8").strip()
    return _line(body)


def forward(socket_path: Path, line: bytes) -> bytes:
    """One request line to the daemon on a new connection; its one reply line back."""
    try:
        check_socket(socket_path)
    except FileNotFoundError:
        return _refusal("transport_unavailable")
    with unix_stream_socket() as stream:
        stream.settimeout(SOCKET_SECONDS)
        try:
            stream.connect(str(socket_path))
        except (FileNotFoundError, ConnectionRefusedError):
            # Nothing is listening: the request never left, so it is safe to retry.
            return _refusal("transport_unavailable")
        stream.sendall(line)
        if not line.endswith(b"\n"):
            # An incomplete or over-long line: end it so the daemon refuses it now.
            stream.shutdown(socket.SHUT_WR)
        with stream.makefile("rb") as response:
            raw = response.readline(RESPONSE_LIMIT)
    if not raw.endswith(b"\n"):
        # The daemon closed without one complete reply line; never pass a torn line on.
        return _refusal("invalid_response")
    return raw


def serve(socket_path: Path, credential: Path | None, stdin: BinaryIO, stdout: BinaryIO) -> int:
    require_local_store()
    if socket_path.exists() or socket_path.is_symlink():
        check_socket(socket_path)
    stdout.write(handshake(credential))
    stdout.flush()
    while True:
        line = stdin.readline(REQUEST_LIMIT + 1)
        if not line:
            return 0
        if len(line) > REQUEST_LIMIT and not line.endswith(b"\n"):
            while True:  # discard the rest of an over-long line
                rest = stdin.readline(65536)
                if not rest or rest.endswith(b"\n"):
                    break
        try:
            reply = forward(socket_path, line)
        except LocalError as exc:
            reply = _refusal(exc.code)
        except OSError:
            reply = _refusal("transport_unavailable")
        stdout.write(reply)
        stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memoryking relay", description=__doc__.split("\n")[0])
    parser.add_argument("--socket", type=Path, required=True, help="The daemon's socket")
    parser.add_argument(
        "--credential", type=Path, help="Private credential file whose token the client uses"
    )
    args = parser.parse_args(argv)
    try:
        return serve(args.socket, args.credential, sys.stdin.buffer, sys.stdout.buffer)
    except LocalError as exc:
        sys.stdout.buffer.write(_refusal(exc.code))
        sys.stdout.buffer.flush()
        return 2
    except (BrokenPipeError, KeyboardInterrupt):
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
