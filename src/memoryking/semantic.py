"""Semantic search in one command: install and verify the pinned model, load it, enable it.

Semantic search is part of the install: `memoryking setup` and `memoryking semantic enable`
are the only things that download the model, and each says what it is about to fetch and
from where before it does (`setup --no-semantic` skips it). A daemon that finds an installed,
verified model in the models directory loads it without a flag. Without the libraries (an
install made with `--no-deps`, or a platform with no wheel for one of them), without a
model, or after a failed download, search stays on keywords and says so.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import urllib.error
from pathlib import Path
from typing import Any

from memoryking.adapters.local_embeddings import MODEL, SUPPORTED, model_files, model_manifest
from memoryking.domain.embeddings import fingerprint
from memoryking.domain.local import LocalError

__all__ = ["MODEL", "SUPPORTED"]

# The models that may be installed and enabled: the default first. Each has a manifest under
# adapters/models/ that pins the repository revision and the SHA-256 of every file.


def models_dir() -> Path:
    """Where models are installed: MEMORYKING_MODELS, else ~/.memoryking/models."""
    override = os.environ.get("MEMORYKING_MODELS")
    return Path(override) if override else Path.home() / ".memoryking" / "models"


def model_dir(name: str = MODEL) -> Path:
    if name not in SUPPORTED:
        raise LocalError("embedding_model_unsupported")
    return models_dir() / name


def name_of(directory: Path) -> str:
    """The manifest a model directory belongs to: its own name when that is a known model."""
    return directory.name if directory.name in SUPPORTED else MODEL


def runtime_problem() -> str | None:
    """None when the embedding libraries are installed at the pinned versions, else why not."""
    configuration = model_manifest(MODEL)
    try:
        for package, version in configuration["runtime"].items():
            if importlib.metadata.version(package) != version:
                return (
                    f"{package} {version} is needed; install the hash-pinned requirements-core.lock"
                )
        for module in ("onnxruntime", "tokenizers", "numpy"):
            importlib.import_module(module)
    except importlib.metadata.PackageNotFoundError:
        return (
            "the embedding libraries (onnxruntime, numpy, tokenizers) are not installed, "
            "as in an install made without its dependencies or on a platform with no wheel "
            "for one of them; `pip install memoryking` or the hash-pinned "
            "requirements-core.lock brings them"
        )
    except (ImportError, OSError) as exc:
        return f"the embedding libraries do not load ({getattr(exc, 'name', None) or exc})"
    return None


def unavailable() -> str | None:
    """Why this process cannot search semantically, or None when it can (libraries and model)."""
    problem = runtime_problem()
    if problem is not None:
        return problem
    if not verified(MODEL):
        return "no model is installed; `memoryking semantic enable` downloads and verifies it"
    return None


def explain(exc: BaseException) -> str:
    """A download failure in plain words: what happened, without a stack trace."""
    if isinstance(exc, LocalError):
        reasons = {
            "model_download_checksum_mismatch": "a downloaded file did not match its pinned "
            "SHA-256, so nothing was kept",
            "model_download_limit": "a download was larger or slower than the pinned manifest allows",
            "model_download_redirect_denied": "the host redirected to a non-HTTPS address, which is refused",
            "model_destination_exists": "a model folder is already there and does not verify; "
            "remove it first",
        }
        return reasons.get(exc.code, f"{exc.code}")
    if isinstance(exc, urllib.error.HTTPError):
        return f"the model host answered HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"the model host could not be reached ({exc.reason})"
    if isinstance(exc, TimeoutError):
        return "the model host did not answer in time"
    return f"{type(exc).__name__}: {exc}"


# What a download can fail with besides a refusal from the verifier.
DOWNLOAD_ERRORS = (LocalError, OSError, ValueError)


def verified(name: str = MODEL) -> bool:
    directory = model_dir(name)
    if not directory.is_dir():
        return False
    try:
        model_files(directory, model_manifest(name))
    except LocalError:
        return False
    return True


def load_installed(threads: int | None = None) -> Any | None:
    """The embedder for the default model when it is installed and the runtime is there."""
    if runtime_problem() is not None or not verified(MODEL):
        return None
    from memoryking.adapters.local_embeddings import LocalOnnxEmbedding

    try:
        return LocalOnnxEmbedding(model_dir(MODEL), MODEL, threads)
    except LocalError:
        return None


def download_size(name: str = MODEL) -> int:
    return sum(file["bytes"] for file in model_manifest(name)["files"])


def consent(name: str = MODEL) -> str:
    """What `enable` states before it fetches anything."""
    manifest = model_manifest(name)
    hosts = sorted({file["url"].split("/")[2] for file in manifest["files"]})
    return (
        f"Downloading {name} ({download_size(name) / 1048576:.0f} MB, licence {manifest['license']}) "
        f"from {', '.join(hosts)} at revision {manifest['revision'][:12]}; every file is verified "
        "against a pinned SHA-256 before it is used. This is the only network request "
        "semantic search makes; it happens once and nothing is fetched after it."
    )


def fingerprint_of(name: str = MODEL) -> str:
    return fingerprint(model_manifest(name))
