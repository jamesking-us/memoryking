#!/usr/bin/env python3
"""Build the release wheel and sdist reproducibly, record their hashes, and prove they install.

Nothing here uploads anything. The package carries the `Private :: Do Not Upload` classifier
until a distribution licence is chosen; this tool produces the artifacts, their SHA-256
manifest and an install line pinned by hash, so a release can be verified before it is
ever published.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode:
        raise SystemExit(f"{' '.join(command)} failed:\n{result.stdout}{result.stderr}")
    return result.stdout


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalise_sdist(path: Path, epoch: int) -> None:
    """Rewrite the sdist with fixed member metadata so one commit yields one archive.

    setuptools writes build-time modification times, owners and a gzip timestamp into the
    tarball; the member contents are already deterministic.
    """
    with tarfile.open(path, "r:gz") as source:
        members = sorted(source.getmembers(), key=lambda m: m.name)
        payloads = {m.name: source.extractfile(m).read() for m in members if m.isfile()}
    with gzip.GzipFile(filename="", mode="wb", fileobj=open(path, "wb"), mtime=0) as raw:
        with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as target:
            for member in members:
                member.mtime = epoch
                member.uid = member.gid = 0
                member.uname = member.gname = ""
                member.pax_headers = {}
                if member.isfile():
                    target.addfile(member, io.BytesIO(payloads[member.name]))
                else:
                    target.addfile(member)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist", help="Artifact directory")
    parser.add_argument(
        "--skip-install", action="store_true", help="Build and hash only; no fresh-venv install"
    )
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    revision = run(["git", "rev-parse", "HEAD"], ROOT).strip()
    committed = run(["git", "show", "-s", "--format=%ct", "HEAD"], ROOT).strip()
    dirty = bool(run(["git", "status", "--porcelain", "--untracked-files=no"], ROOT).strip())
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"PYTHONPATH", "PYTHONHOME", "PYTHONHASHSEED"}
    }
    # A fixed timestamp makes the archives byte-identical across builds of one commit.
    env["SOURCE_DATE_EPOCH"] = committed
    env["PYTHONHASHSEED"] = "0"
    run(
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(output)],
        ROOT,
        env,
    )
    run([sys.executable, "tools/check_wheel.py"], ROOT, env) if output == ROOT / "dist" else None
    artifacts = sorted(p for p in output.iterdir() if p.suffix in {".whl", ".gz"})
    if len(artifacts) != 2:
        raise SystemExit(f"expected one wheel and one sdist in {output}, found {artifacts}")
    normalise_sdist(next(p for p in artifacts if p.suffix == ".gz"), int(committed))
    hashes = {p.name: digest(p) for p in artifacts}
    (output / "SHA256SUMS").write_text(
        "".join(f"{hashes[name]}  {name}\n" for name in sorted(hashes))
    )
    wheel = next(p for p in artifacts if p.suffix == ".whl")
    version = wheel.name.split("-")[1]
    tools = {
        name: run([sys.executable, "-c", f"import {name}; print({name}.__version__)"], ROOT).strip()
        for name in ("build", "setuptools", "wheel")
    }
    manifest = {
        "package": "memoryking",
        "version": version,
        "revision": revision,
        "dirty": dirty,
        "source_date_epoch": int(committed),
        "python": platform.python_version(),
        "tools": tools,
        "artifacts": hashes,
        "install": (
            f"pip install --require-hashes --no-deps {wheel.name} "
            f"--hash=sha256:{hashes[wheel.name]}"
        ),
        "published": False,
        "note": "Not uploaded anywhere; see CONTRIBUTING.md#distribution.",
    }
    installed: dict[str, str] = {}
    if not args.skip_install:
        with tempfile.TemporaryDirectory(prefix="mk-release-") as directory:
            target = Path(directory) / "venv"
            venv.create(target, with_pip=True)
            python = target / ("Scripts" if os.name == "nt" else "bin") / "python"
            pip = [str(python), "-I", "-m", "pip", "--disable-pip-version-check"]
            run([*pip, "install", "-q", "--require-hashes", "-r", "requirements-core.lock"], ROOT)
            # The wheel's own hash pins it exactly as the manifest's install line does.
            requirement = Path(directory) / "release.txt"
            requirement.write_text(f"{wheel.name} --hash=sha256:{hashes[wheel.name]}\n")
            run(
                [*pip, "install", "-q", "--require-hashes", "--no-deps", "-r", str(requirement)],
                output,
            )
            run([*pip, "check"], ROOT)
            installed["version"] = run(
                [str(python), "-I", "-m", "memoryking", "--version"], Path(directory)
            ).strip()
            installed["entry_point"] = str(
                target / ("Scripts" if os.name == "nt" else "bin") / "memoryking"
            )
            if installed["version"].split()[-1] != version:
                raise SystemExit(f"installed {installed['version']} but built {version}")
        manifest["fresh_install"] = installed
    (output / "RELEASE.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
