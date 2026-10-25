#!/usr/bin/env python3
"""Build pinned PostgreSQL in an isolated development directory; never starts a service."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tarfile
import urllib.request
from pathlib import Path

VERSION = "18.6"
BASE = f"https://ftp.postgresql.org/pub/source/v{VERSION}/"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("tmp/postgres-dev"))
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if directory.exists() or not 1 <= args.jobs <= 16:
        raise SystemExit("A new build directory and jobs in 1..16 are required")
    directory.mkdir(parents=True, mode=0o700)
    name = f"postgresql-{VERSION}.tar.bz2"
    with urllib.request.urlopen(BASE + name + ".sha256", timeout=60) as response:
        expected = response.read(1024).decode().split()[0]
    with urllib.request.urlopen(BASE + name, timeout=60) as response:
        raw = response.read(64 * 1024 * 1024 + 1)
    actual = hashlib.sha256(raw).hexdigest()
    if len(raw) > 64 * 1024 * 1024 or actual != expected:
        raise SystemExit("PostgreSQL source checksum mismatch or oversized archive")
    archive = directory / name
    archive.write_bytes(raw)
    with tarfile.open(archive) as bundle:
        bundle.extractall(directory, filter="data")
    source = directory / f"postgresql-{VERSION}"
    prefix = directory / "runtime"
    commands = [
        [
            "./configure",
            f"--prefix={prefix}",
            "--without-icu",
            "--without-readline",
            "--without-zlib",
            "--without-lz4",
            "--without-zstd",
        ],
        ["make", f"-j{args.jobs}"],
        ["make", "install"],
    ]
    evidence = {
        "version": VERSION,
        "source": BASE + name,
        "sha256": actual,
        "prefix": str(prefix),
        "commands": [],
    }
    for i, command in enumerate(commands):
        with (directory / f"{i + 1:02}.log").open("wb") as log:
            result = subprocess.run(
                command, cwd=source, stdout=log, stderr=subprocess.STDOUT, check=False
            )
        evidence["commands"].append({"command": command, "exit_code": result.returncode})
        (directory / "build.json").write_text(json.dumps(evidence, indent=2) + "\n")
        print(("PASS" if result.returncode == 0 else "FAIL") + " " + command[0], flush=True)
        if result.returncode:
            raise SystemExit(result.returncode)
    print(f"Built isolated PostgreSQL {VERSION}: {prefix}/bin; no service started", flush=True)


if __name__ == "__main__":
    main()
