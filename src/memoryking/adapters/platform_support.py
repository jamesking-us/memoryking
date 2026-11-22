"""What differs between operating systems, in one module.

The local store and its daemon rely on POSIX: advisory `flock` locks, owner-only modes
checked against the process uid, a directory `fsync` after every rename, symlink-refusing
opens, and a Unix-domain socket. On Windows the agent-side tools (the client, the CLI, the
MCP stdio bridge, setup and the host hooks) run natively and talk to a store that runs in
WSL2 or in the container image. Every store-side primitive below therefore refuses on
Windows with `local_store_unsupported` instead of running with a weaker guarantee. A
native Windows store would replace these refusals here and nowhere else.

On macOS and Linux every function is exactly the call it replaced.
"""

from __future__ import annotations

import os
import socket
import socketserver
import sys
from types import ModuleType
from typing import Any, NoReturn

from memoryking.domain.local import LocalError

STORE_UNSUPPORTED = "local_store_unsupported"
# True where this machine can hold a store and run the daemon.
LOCAL_STORE_SUPPORTED = sys.platform != "win32"


def require_local_store() -> None:
    """Refuse an operation that needs the store, its control tree or its socket here."""
    if not LOCAL_STORE_SUPPORTED:
        refuse()


def refuse() -> NoReturn:
    raise LocalError(STORE_UNSUPPORTED)


if sys.platform != "win32":
    import fcntl as _fcntl

    # The module itself, for `F_FULLFSYNC` (macOS) in `local_files.durable_sync`.
    fcntl: ModuleType | None = _fcntl

    def owned_by_current_user(uid: int) -> bool:
        return uid == os.getuid()

    def lock_nowait(fd: int) -> None:
        """Take the exclusive advisory lock or raise `BlockingIOError` if another holds it."""
        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)

    def open_nofollow(
        path: str | os.PathLike[str], flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        """`os.open` that refuses a symbolic link as the last component."""
        return os.open(path, flags | os.O_NOFOLLOW, mode, dir_fd=dir_fd)

    def open_directory_nofollow(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> int:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)

    def open_regular_nofollow(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> int:
        """Read-only, refusing a link, and never blocking on a FIFO or device."""
        return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)

    def unix_stream_socket() -> socket.socket:
        return socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    UnixStreamServer = socketserver.UnixStreamServer

    def read_stdin_text() -> str:
        return sys.stdin.read()

else:
    fcntl = None

    def owned_by_current_user(uid: int) -> bool:
        refuse()

    def lock_nowait(fd: int) -> None:
        refuse()

    def open_nofollow(
        path: str | os.PathLike[str], flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        refuse()

    def open_directory_nofollow(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> int:
        refuse()

    def open_regular_nofollow(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> int:
        refuse()

    def unix_stream_socket() -> socket.socket:
        refuse()

    def read_stdin_text() -> str:
        """Piped text as UTF-8, as a text-mode read would see it.

        The console code page (often cp1252) is never what a host or a pipe sends.
        Windows PowerShell prepends a byte-order mark to what it pipes into a program,
        two when its output encoding is set to UTF-8 with one, so every leading mark is
        dropped; CRLF line ends become LF, as in any Windows text-mode read.
        """
        text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        return text.lstrip("﻿").replace("\r\n", "\n")

    class UnixStreamServer(socketserver.TCPServer):
        """Stands in for the Unix-domain server so modules that subclass it still import."""

        def __init__(self, server_address: Any, handler: Any, bind_and_activate: bool = True):
            refuse()
