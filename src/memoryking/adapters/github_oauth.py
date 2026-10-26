"""Bounded fixed GitHub provider adapter. No production test-provider configuration."""

from __future__ import annotations

import os
import re
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

from memoryking.contracts.canonical import canonical_bytes, load_strict
from memoryking.domain.local import LocalError


def secret_file(path: Path) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError
            raw = stream.read(258)
        value = raw.decode("ascii").removesuffix("\n")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", value):
            raise ValueError
        return value
    except (OSError, UnicodeError, ValueError):
        raise LocalError("invalid_viewer_secret") from None


class GitHubProvider:
    def __init__(self, client_id: str, secret: str):
        self.client_id, self._secret = client_id, secret

    def authenticate(self, code: str, verifier: str, callback: str, deadline: float) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LocalError("service_unavailable")
        raw = canonical_bytes(
            {
                "client_id": self.client_id,
                "client_secret": self._secret,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": callback,
            }
        )
        if len(raw) > 8192:
            raise LocalError("invalid_request")
        # An isolated child makes the total bound cover blocking resolver/TLS/header I/O.
        # Environment proxies and Python import overrides cannot redirect this operation.
        try:
            child = subprocess.Popen(
                [sys.executable, "-I", "-m", "memoryking.adapters.github_oauth_worker"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={},
                close_fds=True,
                start_new_session=True,
            )
        except OSError:
            raise LocalError("service_unavailable") from None
        try:
            output, _ = child.communicate(raw, timeout=max(0.001, deadline - time.monotonic()))
            if child.returncode != 0 or len(output) > 256:
                raise LocalError("login_failed")
            result = load_strict(output)
            if (
                not isinstance(result, dict)
                or set(result) != {"user_id"}
                or not isinstance(result["user_id"], str)
                or not re.fullmatch(r"[1-9][0-9]{0,19}", result["user_id"])
            ):
                raise LocalError("login_failed")
            return str(result["user_id"])
        except subprocess.TimeoutExpired:
            raise LocalError("service_unavailable") from None
        except (ValueError, OSError):
            raise LocalError("login_failed") from None
        finally:
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # The child exited between poll and kill; still reap it below.
            child.communicate()  # Join before releasing bounded request capacity.
