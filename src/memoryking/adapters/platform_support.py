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


# File systems a store must not live on: a Windows drive seen from WSL2 (drvfs over 9p),
# NTFS through FUSE or the kernel driver, network shares and VM shared folders. None of
# them gives the owner-only modes and the rename-and-sync durability the store relies on.
FOREIGN_FILESYSTEMS = frozenset(
    {
        "9p",
        "v9fs",
        "drvfs",
        "fuseblk",
        "ntfs",
        "ntfs3",
        "cifs",
        "smb3",
        "smbfs",
        "vboxsf",
        "grpcfuse",
        "fakeowner",  # Docker Desktop's bind mounts of a host folder
    }  # fmt: skip
)


def filesystem_type(path: str | os.PathLike[str]) -> str | None:
    """The type of the file system holding `path` (or its nearest existing parent), read
    from /proc/self/mountinfo; None where that file does not exist (macOS, Windows)."""
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as stream:
            mounts = stream.read().splitlines()
    except OSError:
        return None
    target = os.path.realpath(path)
    while not os.path.exists(target) and os.path.dirname(target) != target:
        target = os.path.dirname(target)
    best, kind = "", None
    for line in mounts:
        fields = line.split()
        if " - " not in line or len(fields) < 5:
            continue
        point = fields[4].replace("\\040", " ")
        after = line.split(" - ", 1)[1].split()
        inside = target == point or target.startswith(point.rstrip("/") + "/") or point == "/"
        if inside and len(point) >= len(best) and after:
            best, kind = point, after[0]
    return kind


# Characters a word may hold to mean the same in Git Bash and in Windows PowerShell,
# unquoted; a word with anything else is double-quoted, which is only safe without these.
# (No comma: PowerShell reads an unquoted `a,b` as an array. No leading `~`: bash expands it.)
SHELL_BARE = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./:=+-~")
SHELL_UNSAFE_IN_QUOTES = frozenset('$`"\\!%')


def neutral_word(word: str) -> bool:
    """A hook command word that both of Claude Code's Windows shells read identically: bare
    safe characters, or one double-quoted string holding none that either shell expands."""
    if word and set(word) <= SHELL_BARE and not word.startswith("~"):
        return True
    inner = word[1:-1]
    return (
        len(word) >= 2
        and word[0] == word[-1] == '"'
        and not set(inner) & SHELL_UNSAFE_IN_QUOTES
        and not set(inner) & {"\n", "\r", "\0"}
    )


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

    def shell_program_path(path: str) -> str:
        return path

    def utf8_stdio() -> None:
        """Nothing to do: the locale's encoding is what POSIX pipes carry."""

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

    def shell_program_path(path: str) -> str:
        """`path` as a first word that Git Bash and PowerShell both run unquoted.

        A quoted first word is a string, not a command, in PowerShell, and `&` means
        something else in bash, so the program is never quoted: forward slashes (a
        backslash is an escape in bash), and the 8.3 short form when the path holds a
        space. Without a short form (8.3 names disabled on that volume) this refuses.
        """
        if " " in path:
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
            buffer = ctypes.create_unicode_buffer(32768)
            if not kernel.GetShortPathNameW(path, buffer, len(buffer)) or " " in buffer.value:
                raise LocalError("hook_command_unsafe", detail={"path": path})
            path = buffer.value
        return path.replace("\\", "/")

    def read_stdin_text() -> str:
        """Piped text as UTF-8, as a text-mode read would see it.

        The console code page (often cp1252) is never what a host or a pipe sends.
        Windows PowerShell prepends a byte-order mark to what it pipes into a program,
        two when its output encoding is set to UTF-8 with one, so every leading mark is
        dropped; CRLF line ends become LF, as in any Windows text-mode read.
        """
        text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        return text.lstrip("﻿").replace("\r\n", "\n")

    def utf8_stdio() -> None:
        """Write stdout and stderr as UTF-8.

        A piped stream otherwise uses the ANSI code page (often cp1252), which cannot
        encode a briefing's characters, and hosts read hook output through a pipe.
        """
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                reconfigure(encoding="utf-8")

    class UnixStreamServer(socketserver.TCPServer):
        """Stands in for the Unix-domain server so modules that subclass it still import."""

        def __init__(self, server_address: Any, handler: Any, bind_and_activate: bool = True):
            refuse()
