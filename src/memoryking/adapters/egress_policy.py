"""The operator's egress policy: which providers, models and hosts retained context may reach.

A policy is a private TOML file loaded by `serve --egress-policy`. Without one, the only
provider is the local recorder and nothing leaves the machine. The daemon enforces the
policy where it cannot be skipped, at inference authorize and consume; the host process
that runs an adapter reads the same file to find its credentials.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from memoryking.adapters.local_files import private_file
from memoryking.domain.local import LocalError

LOCAL_PROVIDER = "local-recording"
LOCAL_MODEL = "echo-v1"
LOCAL_REVISION = "local-recording-v1"
PROVIDERS = ("anthropic-messages", "openai-chat", "vertex-ai")
CLASSIFICATIONS = ("public", "internal", "restricted")
REVISION = re.compile(r"[A-Za-z0-9._-]{1,96}")
MODEL = re.compile(r"[A-Za-z0-9._:@/-]{1,128}")
HOST = re.compile(r"[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?")
REQUEST_BYTES_LIMIT = 98304


def rank(classification: str) -> int:
    """Order classifications so a ceiling can be compared; unknown names rank highest."""
    return CLASSIFICATIONS.index(classification) if classification in CLASSIFICATIONS else 99


def is_loopback(host: str) -> bool:
    name = host.rsplit(":", 1)[0] if re.fullmatch(r".*:[0-9]+", host) else host
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name.strip("[]")).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class ProviderPolicy:
    """One permitted destination. Everything an adapter needs, nothing it may vary."""

    name: str
    host: str
    models: tuple[str, ...]
    max_classification: str = "internal"
    max_request_bytes: int = REQUEST_BYTES_LIMIT
    insecure_http: bool = False
    key_file: Path | None = None
    token_file: Path | None = None
    token_command: tuple[str, ...] | None = None
    path: str | None = None
    publisher: str = "google"
    location: str | None = None
    project: str | None = None
    timeout_s: int = 60
    max_output_tokens: int = 1024
    headers: dict[str, str] = field(default_factory=dict)

    def allows(self, model: str) -> bool:
        return model in self.models


@dataclass(frozen=True)
class EgressPolicy:
    revision: str
    providers: dict[str, ProviderPolicy]
    source: Path | None = None
    fingerprint: str = ""

    @property
    def binding(self) -> str:
        """The revision every conversation binds to: the operator's label plus a digest
        of the policy content, so editing a host or model without a new label still
        retires every conversation instead of letting host and daemon disagree."""
        if not self.fingerprint:
            return self.revision
        return f"{self.revision[:79]}.{self.fingerprint[:16]}"

    @classmethod
    def load(cls, path: Path) -> EgressPolicy:
        private_file(path)
        try:
            data = tomllib.loads(path.read_text())
        except (tomllib.TOMLDecodeError, UnicodeError) as exc:
            raise LocalError("egress_policy_invalid") from exc
        return cls.parse(data, path)

    @classmethod
    def parse(cls, data: Any, source: Path | None = None) -> EgressPolicy:
        if not isinstance(data, dict) or set(data) - {"revision", "providers"}:
            raise LocalError("egress_policy_invalid")
        revision = data.get("revision")
        if not isinstance(revision, str) or not REVISION.fullmatch(revision):
            raise LocalError("egress_policy_invalid")
        if revision == LOCAL_REVISION:
            raise LocalError("egress_policy_invalid")
        entries = data.get("providers", [])
        if not isinstance(entries, list) or len(entries) > 16:
            raise LocalError("egress_policy_invalid")
        providers: dict[str, ProviderPolicy] = {}
        for entry in entries:
            provider = _provider(entry)
            if provider.name in providers:
                raise LocalError("egress_policy_invalid")
            providers[provider.name] = provider
        fingerprint = hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        return cls(revision, providers, source, fingerprint)

    def provider(self, name: str) -> ProviderPolicy | None:
        return self.providers.get(name)


def _provider(entry: Any) -> ProviderPolicy:
    known = {
        "name",
        "host",
        "models",
        "max_classification",
        "max_request_bytes",
        "insecure_http",
        "key_file",
        "token_file",
        "token_command",
        "path",
        "publisher",
        "location",
        "project",
        "timeout_s",
        "max_output_tokens",
        "headers",
    }
    if not isinstance(entry, dict) or set(entry) - known:
        raise LocalError("egress_policy_invalid")
    name = entry.get("name")
    if name not in PROVIDERS:
        raise LocalError("egress_policy_invalid")
    models = entry.get("models")
    if (
        not isinstance(models, list)
        or not models
        or len(models) > 64
        or any(not isinstance(m, str) or not MODEL.fullmatch(m) for m in models)
    ):
        raise LocalError("egress_policy_invalid")
    location = _optional_str(entry, "location", r"[a-z0-9-]{1,64}")
    project = _optional_str(entry, "project", r"[a-z][a-z0-9-]{4,29}")
    host = entry.get("host")
    if host is None and name == "vertex-ai" and location:
        host = f"{location}-aiplatform.googleapis.com"
    if not isinstance(host, str) or not HOST.fullmatch(host):
        raise LocalError("egress_policy_invalid")
    insecure = entry.get("insecure_http", False)
    if not isinstance(insecure, bool) or (insecure and not is_loopback(host)):
        # Plain HTTP is for a model server on this machine only, never for a remote host.
        raise LocalError("egress_policy_invalid")
    classification = entry.get("max_classification", "internal")
    if classification not in CLASSIFICATIONS:
        raise LocalError("egress_policy_invalid")
    size = entry.get("max_request_bytes", REQUEST_BYTES_LIMIT)
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= REQUEST_BYTES_LIMIT:
        raise LocalError("egress_policy_invalid")
    timeout = entry.get("timeout_s", 60)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 300:
        raise LocalError("egress_policy_invalid")
    output = entry.get("max_output_tokens", 1024)
    if isinstance(output, bool) or not isinstance(output, int) or not 1 <= output <= 65536:
        raise LocalError("egress_policy_invalid")
    publisher = entry.get("publisher", "google")
    if publisher not in {"google", "anthropic"}:
        raise LocalError("egress_policy_invalid")
    if name == "vertex-ai" and not (location and project):
        raise LocalError("egress_policy_invalid")
    command = entry.get("token_command")
    if command is not None and (
        not isinstance(command, list)
        or not command
        or len(command) > 16
        or any(not isinstance(c, str) or not c for c in command)
        or not Path(command[0]).is_absolute()
    ):
        raise LocalError("egress_policy_invalid")
    headers = entry.get("headers", {})
    if (
        not isinstance(headers, dict)
        or len(headers) > 8
        or any(
            not isinstance(k, str)
            or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", k)
            or k.lower() in {"authorization", "x-api-key", "host", "content-length"}
            or not isinstance(v, str)
            or len(v) > 256
            for k, v in headers.items()
        )
    ):
        raise LocalError("egress_policy_invalid")
    return ProviderPolicy(
        name=name,
        host=host,
        models=tuple(models),
        max_classification=classification,
        max_request_bytes=size,
        insecure_http=insecure,
        key_file=_optional_path(entry, "key_file"),
        token_file=_optional_path(entry, "token_file"),
        token_command=tuple(command) if command else None,
        path=_optional_str(entry, "path", r"/[A-Za-z0-9._/:-]{0,255}"),
        publisher=publisher,
        location=location,
        project=project,
        timeout_s=timeout,
        max_output_tokens=output,
        headers=dict(headers),
    )


def _optional_str(entry: dict[str, Any], key: str, pattern: str) -> str | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise LocalError("egress_policy_invalid")
    return value


def _optional_path(entry: dict[str, Any], key: str) -> Path | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise LocalError("egress_policy_invalid")
    return Path(value)
