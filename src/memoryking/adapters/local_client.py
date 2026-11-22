"""The client end of the local daemon's socket, with nothing of the store imported.

A hook, the relay and the SDK send requests through `request`; importing the daemon's
module (`local_ipc`) would load every store adapter first, which a short-lived hook pays
for on every call. `local_ipc` re-exports these names unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from memoryking.adapters.platform_support import unix_stream_socket
from memoryking.contracts.canonical import canonical_bytes, load_strict
from memoryking.domain.local import LocalError
from memoryking.relay_client import RelayEndpoint

WIRE_LIMIT = 262144
# Public fields a refusal may carry beside `error` and `hint`.
REPLY_DETAIL = ("required_capability", "path", "problem", "expected", "missing")


# Where a client sends requests: the daemon's socket, or a relay to it (relay_client).
Socket = Path | RelayEndpoint


def request(
    socket_path: Socket, credential: str, operation: str, args: dict[str, Any]
) -> dict[str, Any]:
    if isinstance(socket_path, RelayEndpoint):
        return socket_path.request(credential, operation, args)
    payload = canonical_bytes(
        {"version": 1, "credential": credential, "operation": operation, "args": args}
    )
    if len(payload) + 1 > WIRE_LIMIT:
        raise LocalError("invalid_request")
    with unix_stream_socket() as stream:
        stream.settimeout(10)
        try:
            stream.connect(str(socket_path))
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            # Nothing is listening: the request never left this process, so it is safe to retry.
            raise LocalError("transport_unavailable") from exc
        stream.sendall(payload + b"\n")
        with stream.makefile("rb") as response:
            raw = response.readline(1048577)
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
