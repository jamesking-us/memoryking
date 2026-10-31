"""HTTP variant of the project SDK; no automatic mutation retry or redirect following."""

from __future__ import annotations

import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from memoryking.adapters.local_store import identifier
from memoryking.contracts.canonical import canonical_bytes, load_strict
from memoryking.domain.local import LocalError
from memoryking.sdk import BINDING_FIELDS, LocalClient


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        raise LocalError("redirect_forbidden")


class SharedClient(LocalClient):
    def __init__(
        self,
        url: str,
        tenant: str,
        credential_file: Path,
        project: str,
        *,
        ca_file: Path | None = None,
        loopback_http: bool = False,
        namespace: str = "sdk",
    ):
        self.namespace = namespace
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or not parsed.hostname
        ):
            raise LocalError("invalid_shared_url")
        if parsed.scheme != "https" and not (
            loopback_http
            and parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        ):
            raise LocalError("shared_tls_required")
        self.url = url.rstrip("/") + "/v1/tenants/" + identifier(tenant) + "/operations"
        self.credential_file, self.project = credential_file, identifier(project)
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            NoRedirect(),
            urllib.request.HTTPSHandler(
                context=ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
            ),
        )
        self.last_context = self._request(self._credential(), "context", {"project": project})
        self.binding = tuple(self.last_context[key] for key in BINDING_FIELDS)
        self.delegation = self.last_context.get("delegation_chain", [])

    def _current(self) -> tuple[str, dict[str, Any]]:
        credential, context = super()._current()
        if context.get("delegation_chain", []) != self.delegation:
            raise LocalError("context_changed")
        return credential, context

    def _request(self, credential: str, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        raw = canonical_bytes({"version": 1, "operation": operation, "args": args})
        if len(raw) > 262144:
            raise LocalError("invalid_request")
        request = urllib.request.Request(
            self.url,
            raw,
            headers={
                "Authorization": "Bearer " + credential,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=10) as response:
                body = response.read(1048577)
        except urllib.error.HTTPError as exc:
            body = exc.read(1048577)
        except OSError as exc:
            raise LocalError("transport_unavailable") from exc
        if len(body) > 1048576:
            raise LocalError("invalid_response")
        value = load_strict(body)
        if not isinstance(value, dict):
            raise LocalError("invalid_response")
        if "error" in value:
            raise LocalError(value["error"])
        return value
