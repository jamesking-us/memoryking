"""Semantic search in one command: install and verify the pinned model, load it, enable it.

Nothing here runs on its own. `memoryking semantic enable` (and `memoryking setup` when the
optional `embeddings` extra is installed) is the only thing that downloads the model, and
it says what it is about to fetch and from where before it does. A daemon that finds an
installed, verified model in the models directory loads it without a flag; without one, or
without the extra, briefings stay lexical and say so.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import os
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
    """None when the optional runtime is installed at the pinned versions, else what to do."""
    configuration = model_manifest(MODEL)
    try:
        for package, version in configuration["runtime"].items():
            if importlib.metadata.version(package) != version:
                return f"{package} {version} is needed; install the hash-pinned requirements-embeddings.lock"
        for module in ("onnxruntime", "tokenizers", "numpy"):
            importlib.import_module(module)
    except importlib.metadata.PackageNotFoundError:
        return (
            "the optional embeddings extra is not installed: "
            "`pip install 'memoryking[embeddings]'` (or the hash-pinned requirements-embeddings.lock)"
        )
    except ImportError as exc:
        return f"the embeddings runtime does not load ({exc.name})"
    return None


def verified(name: str = MODEL) -> bool:
    directory = model_dir(name)
    if not directory.is_dir():
        return False
    try:
        model_files(directory, model_manifest(name))
    except LocalError:
        return False
    return True


def load_installed(threads: int = 1) -> Any | None:
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
        "semantic search makes, and MemoryKing never downloads a model on its own."
    )


def fingerprint_of(name: str = MODEL) -> str:
    return fingerprint(model_manifest(name))
