"""Explicit download of the bundled public model manifest; never runs during inference."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
import time
import urllib.request
from pathlib import Path

from memoryking.adapters.local_embeddings import MODEL, RERANKER, model_files, model_manifest
from memoryking.domain.embeddings import fingerprint
from memoryking.domain.local import LocalError


class HTTPSOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if not newurl.startswith("https://"):
            raise LocalError("model_download_redirect_denied")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def install(destination: Path, model: str = MODEL) -> dict[str, str]:
    manifest = model_manifest(model)
    if destination.exists():
        model_files(destination, manifest)
        return {"status": "already_verified", "model": model, "fingerprint": fingerprint(manifest)}
    destination.parent.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(HTTPSOnly())
    with tempfile.TemporaryDirectory(prefix=".model-download-", dir=destination.parent) as temp:
        staging = Path(temp) / "model"
        staging.mkdir(mode=0o700)
        for file in manifest["files"]:
            path = staging / file["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            digest, received = hashlib.sha256(), 0
            started = time.monotonic()
            with opener.open(file["url"], timeout=30) as response, path.open("xb") as output:
                while chunk := response.read(65536):
                    received += len(chunk)
                    if received > file["bytes"] or time.monotonic() - started > 180:
                        raise LocalError("model_download_limit")
                    digest.update(chunk)
                    output.write(chunk)
            if received != file["bytes"] or digest.hexdigest() != file["sha256"]:
                raise LocalError("model_download_checksum_mismatch")
        model_files(staging, manifest)
        # All files are checked before the destination becomes available.
        if destination.exists():
            raise LocalError("model_destination_exists")
        shutil.move(str(staging), destination)
    return {"status": "installed_verified", "model": model, "fingerprint": fingerprint(manifest)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument(
        "--model",
        choices=["embedding", "reranker"],
        default="embedding",
        help="Which pinned manifest to install: the embedding model (default) or the reranker",
    )
    args = parser.parse_args()
    try:
        model = RERANKER if args.model == "reranker" else MODEL
        print(json.dumps(install(args.destination.resolve(), model), sort_keys=True))
    except Exception as exc:
        raise SystemExit(
            exc.code if isinstance(exc, LocalError) else "model_download_failed"
        ) from None


if __name__ == "__main__":
    main()
