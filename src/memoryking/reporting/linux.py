"""Linux x86-64 renderer confinement: Landlock ABI >=6 plus syscall allowlist.

Installed only by the fixed trusted worker, before parsing report bytes or loading
optional renderer libraries. Unsupported kernels/architectures fail closed.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import signal
import sys
from pathlib import Path
from typing import Sequence

import memoryking
from memoryking.domain.local import LocalError


class Ruleset(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


class Beneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class Filter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint32),
    ]


class Program(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(Filter))]


# Linux x86-64 syscall ABI; default is EPERM, including future unknown syscalls.
# No socket, exec, process creation, ptrace, cross-process memory, mount, keyring,
# async I/O, ownership/permission changes or signal-to-another-process operations.
ALLOWED = frozenset(
    {
        0,
        1,
        2,
        3,
        4,
        5,
        6,
        8,
        9,
        10,
        11,
        12,
        13,
        14,
        15,
        17,
        19,
        20,
        21,
        23,
        24,
        25,
        28,
        32,
        33,
        35,
        37,
        38,
        39,
        60,
        63,
        72,
        74,
        75,
        79,
        80,
        81,
        82,
        83,
        84,
        85,
        86,
        87,
        88,
        89,
        95,
        96,
        97,
        98,
        99,
        102,
        104,
        107,
        108,
        110,
        131,
        137,
        138,
        160,
        186,
        202,
        204,
        217,
        228,
        229,
        230,
        231,
        257,
        258,
        262,
        263,
        264,
        265,
        266,
        267,
        269,
        270,
        271,
        292,
        318,
        332,
    }
)


def filter_program() -> list[tuple[int, int, int, int]]:
    denied = 0x00050000 | errno.EPERM
    instructions = [
        (0x20, 0, 0, 4),  # seccomp_data.arch
        (0x15, 1, 0, 0xC000003E),  # AUDIT_ARCH_X86_64; other ABI kills
        (0x06, 0, 0, 0x80000000),
        (0x20, 0, 0, 0),  # seccomp_data.nr
        (0x35, 0, 1, 0x40000000),  # reject x32 alternate syscall numbering
        (0x06, 0, 0, denied),
    ]
    for number in sorted(ALLOWED):
        instructions.extend([(0x15, 0, 1, number), (0x06, 0, 0, 0x7FFF0000)])
    # prlimit64 is permitted only for pid=0 (this process). Hard limits cannot rise.
    instructions.extend(
        [
            (0x15, 0, 4, 302),
            (0x20, 0, 0, 16),
            (0x15, 0, 1, 0),
            (0x06, 0, 0, 0x7FFF0000),
            (0x06, 0, 0, denied),
            (0x06, 0, 0, denied),
        ]
    )
    return instructions


def seal(stage: Path, *, expected_parent: int, protected: Sequence[Path] = ()) -> None:
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise LocalError("report_sandbox_unavailable")
    stage = stage.resolve(strict=True)
    roots = {
        Path(sys.base_prefix).resolve(),
        Path(sys.prefix).resolve(),
        Path(memoryking.__file__).resolve().parent,
        Path("/usr/lib").resolve(),
        Path("/lib").resolve(),
        Path("/usr/share/fonts"),
        Path("/usr/share/zoneinfo"),
    }
    roots = {root for root in roots if root.is_dir()}
    for private in protected:
        private = private.resolve()
        if any(
            private.is_relative_to(root) or root.is_relative_to(private) for root in roots | {stage}
        ):
            raise LocalError("unsafe_renderer_layout")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    abi = libc.syscall(444, None, 0, 1)  # landlock_create_ruleset ABI query
    if abi < 6:
        raise LocalError("report_sandbox_unavailable")
    # An orphan must die with its parent; recheck the race before parsing input.
    if (
        os.getppid() != expected_parent
        or libc.prctl(1, int(signal.SIGKILL), 0, 0, 0) != 0
        or os.getppid() != expected_parent
    ):
        raise LocalError("report_sandbox_unavailable")
    for number in os.listdir("/proc/self/fd"):
        if int(number) > 2:
            try:
                os.close(int(number))
            except OSError:
                pass
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise LocalError("report_sandbox_unavailable")
    # All filesystem rights through ABI5 ioctl restrictions; ABI6 scopes also
    # prohibit abstract-UNIX IPC and signals across the sandbox boundary.
    rules = Ruleset((1 << 16) - 1, 0, 3)
    descriptor = libc.syscall(444, ctypes.byref(rules), ctypes.sizeof(rules), 0)
    if descriptor < 0:
        raise LocalError("report_sandbox_unavailable")
    try:

        def allow(path: Path, access: int) -> None:
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = Beneath(access, fd)
                if libc.syscall(445, descriptor, 1, ctypes.byref(rule), 0) != 0:
                    raise LocalError("report_sandbox_unavailable")
            finally:
                os.close(fd)

        for root in roots:
            allow(root, (1 << 2) | (1 << 3))  # READ_FILE, READ_DIR, never execute/write
        # Stage permits regular files/directories only; no device, socket, FIFO or symlink creation.
        stage_access = sum(1 << bit for bit in (1, 2, 3, 4, 5, 7, 8, 13, 14))
        allow(stage, stage_access)
        for path in (Path("/dev/urandom"), Path("/dev/random")):
            if path.exists():
                allow(path, 1 << 2)
        allow(Path("/dev/null"), (1 << 1) | (1 << 2))
        if libc.syscall(446, descriptor, 0) != 0:
            raise LocalError("report_sandbox_unavailable")
    finally:
        os.close(descriptor)
    instructions = filter_program()
    array = (Filter * len(instructions))(*(Filter(*item) for item in instructions))
    program = Program(len(instructions), array)
    if libc.prctl(22, 2, ctypes.byref(program), 0, 0) != 0:
        raise LocalError("report_sandbox_unavailable")
