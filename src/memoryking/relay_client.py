"""The client end of `memoryking relay`: one relay process, many requests.

A client that cannot reach the daemon's Unix socket (a Windows client whose store is in
WSL2 or the container image) starts the relay on the store's side through an explicit
command, `wsl.exe -d DISTRO -- ...` or `docker exec -i CONTAINER ...`, and keeps it
open: the start-up is paid once per connection, not per request. The pipe to that
process is the only channel; there is no listener and no new secret.

The relay's first line is its handshake. A relay of a different MemoryKing version is
refused with `relay_version_mismatch` before any request is sent, so a client and a
daemon of different versions never meet as a protocol failure. When the relay was
started with a credential file, the token arrives in the handshake and is held only in
this process's memory; it is never written on this side.

Requests have the same shape, limits and refusals as `local_ipc.request`, and a request
is never retried: if the relay ends after a request was written, the outcome is unknown
and the caller is told so (`relay_interrupted`), exactly like a socket that closes after
the request left.
"""

from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
from pathlib import PurePath
from typing import IO, Any

from memoryking import __version__
from memoryking.contracts.canonical import canonical_bytes, load_strict
from memoryking.domain.local import LocalError

PROTOCOL = 1
REQUEST_LIMIT = 262144  # local_ipc.WIRE_LIMIT, checked by tests
RESPONSE_LIMIT = 1048577  # what local_ipc.request reads, checked by tests
START_SECONDS = 30.0
# The relay waits up to 10 s for the daemon; allow for that plus the pipe.
REPLY_SECONDS = 20.0
REPLY_DETAIL = ("required_capability", "path", "problem", "expected", "missing")


def missing_extras(theirs: Any) -> list[str]:
    """Extras this side uses that a relay's handshake says the store side lacks. A relay
    that names no extras (an older one) is not refused for it."""
    if not isinstance(theirs, list):
        return []
    import importlib.util

    ours = ["tokenizers"] if importlib.util.find_spec("tiktoken") is not None else []
    return [extra for extra in ours if extra not in theirs]


def _parse(line: bytes) -> Any:
    try:
        return json.loads(line)
    except ValueError:
        return None


class RelayEndpoint:
    """A running relay: send `request` lines, read the daemon's replies."""

    def __init__(self, argv: list[str], *, start_seconds: float = START_SECONDS):
        self.argv = list(argv)
        self.lock = threading.Lock()
        try:
            self.process = subprocess.Popen(
                self.argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as exc:
            raise LocalError("relay_unavailable") from exc
        assert self.process.stdin is not None and self.process.stdout is not None
        self.stdin: IO[bytes] = self.process.stdin
        self.lines: queue.Queue[bytes] = queue.Queue()
        threading.Thread(target=self._read, args=(self.process.stdout,), daemon=True).start()
        first = self._next(start_seconds, "relay_unavailable")
        # The line may hold the token, and a parse error keeps the whole line: raise outside
        # the except block so no exception that carries it is attached to the refusal.
        hello = _parse(first)
        if not isinstance(hello, dict):
            self.close()
            raise LocalError("relay_unavailable")
        if "error" in hello:
            self.close()
            raise LocalError(str(hello["error"]))
        if hello.get("relay") != PROTOCOL or hello.get("version") != __version__:
            self.close()
            raise LocalError(
                "relay_version_mismatch",
                detail={"expected": __version__, "found": str(hello.get("version"))},
            )
        missing = missing_extras(hello.get("extras"))
        if missing:
            # Briefings here would ask for exact token counts the store side cannot make;
            # say so now, not as tokenizer_unavailable in the middle of a session.
            self.close()
            raise LocalError(
                "store_extras_mismatch", detail={"missing_on": "store", "extra": missing[0]}
            )
        credential = hello.get("credential")
        self.credential: str | None = credential if isinstance(credential, str) else None

    def _read(self, stream: IO[bytes]) -> None:
        while True:
            line = stream.readline(RESPONSE_LIMIT)
            self.lines.put(line)
            if not line:
                return

    def _next(self, seconds: float, code: str) -> bytes:
        try:
            line = self.lines.get(timeout=seconds)
        except queue.Empty:
            self.close()
            raise LocalError(code) from None
        if not line.endswith(b"\n"):
            self.close()
            raise LocalError(code)
        return line

    def request(self, credential: str, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        payload = canonical_bytes(
            {"version": 1, "credential": credential, "operation": operation, "args": args}
        )
        if len(payload) + 1 > REQUEST_LIMIT:
            raise LocalError("invalid_request")
        with self.lock:
            if self.process.poll() is not None:
                # Nothing was sent: safe to retry after starting a new relay.
                raise LocalError("relay_unavailable")
            try:
                self.stdin.write(payload + b"\n")
                self.stdin.flush()
            except OSError as exc:
                raise LocalError("relay_unavailable") from exc
            raw = self._next(REPLY_SECONDS, "relay_interrupted")
        value = load_strict(raw)
        if not isinstance(value, dict):
            raise LocalError("invalid_response")
        if "error" in value:
            raise LocalError(
                value["error"],
                value.get("hint"),
                {k: v for k, v in value.items() if k in REPLY_DETAIL},
            )
        return value

    def close(self) -> None:
        try:
            self.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        if self.process.stdout is not None:
            self.process.stdout.close()

    def __enter__(self) -> RelayEndpoint:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class RelayCredential:
    """The token a relay handed over, in place of a credential file on this side.

    It exists only in this process's memory and never prints its value.
    """

    def __init__(self, endpoint: RelayEndpoint):
        self.endpoint = endpoint

    @property
    def token(self) -> str:
        if self.endpoint.credential is None:
            raise LocalError("credential_required")
        return self.endpoint.credential

    def __repr__(self) -> str:
        return "RelayCredential(<in memory>)"

    __str__ = __repr__


NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


# Where `memoryking setup` installs MemoryKing inside a WSL2 distro: on the distro's own
# file system, in the user's home, so it is found without anything on the command line.
WSL_PYTHON = "$HOME/.local/share/memoryking/venv/bin/python"


def relay_argv(
    spec: str,
    socket: str | None,
    credential: str | None,
    python: str | None,
    profile: str | None = None,
) -> list[str]:
    """The command that starts the relay on the store's side, chosen explicitly.

    `spec` is `wsl:DISTRO` or `docker:CONTAINER`; nothing is inferred. The relay finds the
    daemon either from `profile`, a named profile on that side (socket, credential, and
    start on demand), or from `socket` and `credential`, paths on that side. No shell on
    this side ever sees the command (`wsl.exe --exec`, `docker exec`). Without `python`,
    WSL2 runs the interpreter setup installs (`WSL_PYTHON`, expanded by `sh` there) and the
    container its own `python`.
    """
    kind, _, name = spec.partition(":")
    if kind not in {"wsl", "docker"} or not NAME.fullmatch(name):
        raise LocalError("relay_invalid")
    if profile is not None and not NAME.fullmatch(profile):
        raise LocalError("relay_invalid")
    if profile is None and socket is None:
        raise LocalError("socket_required")
    for path in (socket, credential, python):
        if path is not None and (not path.startswith("/") or "\n" in path or "\0" in path):
            if path is python and "/" not in path:
                continue  # a command name such as python3, looked up on that side
            raise LocalError("relay_invalid")
    tail = ["-m", "memoryking.relay"]
    if profile is not None:
        tail += ["--profile", profile]
    if socket is not None:
        tail += ["--socket", socket]
    if credential is not None:
        tail += ["--credential", credential]
    if kind == "docker":
        return ["docker", "exec", "-i", name, python or "python", *tail]
    if python is not None:
        return ["wsl.exe", "-d", name, "--exec", python, *tail]
    script = f'exec "{WSL_PYTHON}" "$@"'
    return ["wsl.exe", "-d", name, "--exec", "/bin/sh", "-c", script, "sh", *tail]


def remote(path: Any) -> str | None:
    """A path given on this side's command line, as the store side spells it."""
    if path is None:
        return None
    return PurePath(path).as_posix()


def connect(
    spec: str,
    socket: Any,
    credential: Any,
    python: str | None = None,
    profile: str | None = None,
) -> tuple[RelayEndpoint, RelayCredential]:
    """Start a relay and return it with the credential it handed over."""
    argv = relay_argv(spec, remote(socket), remote(credential), python, profile)
    endpoint = RelayEndpoint(argv)
    return endpoint, RelayCredential(endpoint)


def credential_token(credential: Any) -> str:
    """The token to send: from a relay's handshake, or from a private credential file."""
    if isinstance(credential, RelayCredential):
        return credential.token
    from memoryking.adapters.local_files import private_file

    private_file(credential)
    return str(credential.read_text(encoding="utf-8")).strip()
