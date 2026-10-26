"""Fixed-endpoint OAuth exchange child. Secrets enter stdin, never argv or logs.

The parent terminates this process at the total deadline, including stalled DNS.
No endpoint, proxy, CA or executable can be selected by the request/configuration.
"""

from __future__ import annotations

import http.client
import re
import ssl
import sys
from typing import Any, cast
from urllib.parse import urlencode

from memoryking.contracts.canonical import canonical_bytes, load_strict

MAX_BODY = 65536


class HeaderReader:
    def __init__(self, source: Any):
        self.source, self.remaining, self.lines = source, 16384, 0

    def readline(self, size: int = -1) -> bytes:
        if self.remaining <= 0 or self.lines >= 64:
            raise ValueError
        line: bytes = self.source.readline(min(size if size >= 0 else 16385, self.remaining + 1))
        self.remaining -= len(line)
        self.lines += 1
        if self.remaining < 0:
            raise ValueError
        return line


class BoundedResponse(http.client.HTTPResponse):
    def begin(self) -> None:
        source = self.fp
        self.fp = cast(Any, HeaderReader(source))
        try:
            super().begin()
        finally:
            self.fp = source


def fetch(host: str, path: str, body: bytes | None, headers: dict[str, str]) -> dict[str, Any]:
    connection = http.client.HTTPSConnection(host, timeout=5, context=ssl.create_default_context())
    connection.response_class = BoundedResponse
    try:
        connection.request("POST" if body is not None else "GET", path, body=body, headers=headers)
        response = connection.getresponse()
        # No redirect following, transparent decompression or response-body diagnostics.
        if response.status != 200:
            raise ValueError
        for name in ("Content-Length", "Content-Type", "Content-Encoding", "X-OAuth-Scopes"):
            values = response.headers.get_all(name, [])
            if len(values) > 1:
                raise ValueError
        if response.headers.get("Content-Encoding", "identity") != "identity":
            raise ValueError
        if response.headers.get_content_type() != "application/json":
            raise ValueError
        length = response.headers.get("Content-Length")
        if length is not None and (not length.isdecimal() or int(length) > MAX_BODY):
            raise ValueError
        if response.headers.get("X-OAuth-Scopes", "").strip():
            raise ValueError
        raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise ValueError
        value = load_strict(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    finally:
        connection.close()


def exchange(value: dict[str, str]) -> str:
    if set(value) != {"client_id", "client_secret", "code", "code_verifier", "redirect_uri"}:
        raise ValueError
    token = fetch(
        "github.com",
        "/login/oauth/access_token",
        urlencode(value).encode("ascii"),
        {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "MemoryKing-viewer",
        },
    )
    if token.get("token_type") != "bearer" or token.get("scope") != "":
        raise ValueError
    access = token.get("access_token")
    if not isinstance(access, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,2048}", access):
        raise ValueError
    user = fetch(
        "api.github.com",
        "/user",
        None,
        {
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + access,
            "User-Agent": "MemoryKing-viewer",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    )
    identifier = user.get("id")
    if (
        type(identifier) is not int
        or not 1 <= identifier <= 9007199254740991
        or user.get("type") != "User"
    ):
        raise ValueError
    return str(identifier)


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(8193)
        if len(raw) > 8192:
            raise ValueError
        value = load_strict(raw)
        if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
            raise ValueError
        identifier = exchange(value)
        sys.stdout.buffer.write(canonical_bytes({"user_id": identifier}))
        return 0
    except Exception:
        return 1  # No exception text, provider body, token or callback query leaves this child.


if __name__ == "__main__":
    raise SystemExit(main())
