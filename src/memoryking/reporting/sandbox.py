"""Fail-closed local renderer process boundary; never receives service credentials."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

import memoryking
from memoryking.domain.local import LocalError
from memoryking.domain.reporting import BUILD_TIMEOUT, INPUT_LIMIT

RSS_LIMIT_KIB = 512 * 1024
MAX_INPUT_FILES = 64


def macos_profile(stage: Path, protected: Sequence[Path] = ()) -> str:
    """Literal ancestor directories allow dyld traversal, never their file contents."""
    stage = stage.resolve(strict=True)
    package = Path(memoryking.__file__).resolve().parent
    read_roots = {
        Path(sys.base_prefix).resolve(),
        Path(sys.prefix).resolve(),
        package,
        Path("/System/Library"),
        Path("/usr/lib"),
        Path("/Library/Apple/System/Library"),
        Path("/System/Volumes/Preboot"),
        Path("/private/var/db/dyld"),
    }
    for private in protected:
        private = private.resolve()
        if any(private.is_relative_to(root) or root.is_relative_to(private) for root in read_roots):
            raise LocalError("unsafe_renderer_layout")
        if private.is_relative_to(stage):
            raise LocalError("unsafe_renderer_layout")
    read_roots.add(stage)
    parents = {parent for root in read_roots for parent in root.parents}

    def quoted(path: Path) -> str:
        return json.dumps(str(path))

    reads = " ".join(f"(subpath {quoted(p)})" for p in sorted(read_roots))
    executable_reads = " ".join(f"(subpath {quoted(p)})" for p in sorted(read_roots - {stage}))
    ancestors = " ".join(f"(literal {quoted(p)})" for p in sorted(parents))
    executable = Path(sys.executable)
    return "\n".join(
        [
            "(version 1)",
            "(deny default)",
            "(allow sysctl-read)",
            "(allow file-read-metadata)",
            '(allow file-read* (literal "/dev/urandom") (literal "/dev/random") (literal "/dev/null"))',
            f"(allow file-read* {reads} {ancestors})",
            f"(allow file-map-executable {executable_reads})",
            f'(allow file-write* (subpath {quoted(stage)}) (literal "/dev/null"))',
            f"(allow process-exec (literal {quoted(executable)}) (literal {quoted(executable.resolve())}))",
        ]
    )


def isolated_python(
    stage: Path,
    arguments: Sequence[str],
    *,
    protected: Sequence[Path] = (),
    timeout: float = BUILD_TIMEOUT,
    rss_limit_kib: int = RSS_LIMIT_KIB,
) -> None:
    """Run a trusted Python entry point. Report content must never supply arguments.

    Linux isolation is deliberately unavailable until its separate implementation
    and negative tests exist. A platform failure never falls back to an open process.
    """
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise LocalError("report_sandbox_unavailable")
    stage = stage.resolve(strict=True)
    if not stage.is_dir() or stage.stat().st_mode & 0o077:
        raise LocalError("unsafe_permissions")
    size = 0
    for count, path in enumerate(stage.rglob("*"), 1):
        if count > MAX_INPUT_FILES:
            raise LocalError("report_input_limit")
        if path.is_symlink() or not path.is_file():
            raise LocalError("invalid_report_input")
        size += path.stat().st_size
    if size > INPUT_LIMIT:
        raise LocalError("report_input_limit")
    command = [
        "/usr/bin/sandbox-exec",
        "-p",
        macos_profile(stage, protected),
        sys.executable,
        "-I",
        "-B",
        *arguments,
    ]
    # No shell startup environment, mail settings, database descriptors or stdin.
    environment = {"HOME": str(stage), "TMPDIR": str(stage), "PATH": "/usr/bin:/bin"}
    process = subprocess.Popen(
        command,
        cwd=stage,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    started = time.monotonic()
    try:
        while process.poll() is None:
            if time.monotonic() - started >= timeout:
                raise LocalError("report_render_timeout")
            usage = subprocess.run(
                ["/bin/ps", "-o", "rss=", "-p", str(process.pid)],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if usage.returncode == 0 and usage.stdout.strip():
                if int(usage.stdout.strip()) > rss_limit_kib:
                    raise LocalError("report_render_memory_limit")
            elif process.poll() is None:
                raise LocalError("report_resource_monitor_unavailable")
            time.sleep(0.05)
        if process.returncode:
            raise LocalError("report_render_failed")
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()


def render(stage: Path, *, protected: Sequence[Path] = ()) -> None:
    isolated_python(
        stage, ["-m", "memoryking.reporting.worker", str(stage.resolve())], protected=protected
    )
