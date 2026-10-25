#!/usr/bin/env python3
"""Record installed metadata and foundation output sizes without fetching dependencies."""

import gzip
import hashlib
import importlib.metadata
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def collect():
    python = []
    for distribution in importlib.metadata.distributions():
        metadata = distribution.metadata
        if metadata["Name"].lower() == "memoryking":
            continue
        license_name = metadata.get("License-Expression") or metadata.get("License")
        if not license_name or len(license_name) > 120:
            license_name = (
                "; ".join(
                    entry.removeprefix("License :: ")
                    for entry in metadata.get_all("Classifier", [])
                    if entry.startswith("License ::")
                )
                or "See installed license files"
            )
        python.append(
            {
                "name": metadata["Name"],
                "version": metadata["Version"],
                "license": license_name,
                "license_files": [
                    {
                        "path": str(path),
                        "sha256": hashlib.sha256(
                            distribution.locate_file(path).read_bytes()
                        ).hexdigest(),
                    }
                    for path in distribution.files or []
                    if any(word in path.name.lower() for word in ("license", "copying", "notice"))
                    and distribution.locate_file(path).is_file()
                    and distribution.locate_file(path).stat().st_size <= 8 * 1024 * 1024
                ],
            }
        )
    lock = json.loads((ROOT / "package-lock.json").read_text())
    node = []
    for path, entry in lock["packages"].items():
        if not path.startswith("node_modules/") or entry.get("link"):
            continue
        installed = ROOT / path / "package.json"
        metadata = json.loads(installed.read_text()) if installed.exists() else entry
        node.append(
            {
                "name": metadata.get("name", path.removeprefix("node_modules/")),
                "version": metadata["version"],
                "license": metadata.get("license", "See package license files"),
                "integrity": entry.get("integrity"),
                "installed": installed.exists(),
                "optional_platform": entry.get("optional", False),
            }
        )
    outputs = []
    for package in ("contracts", "dashboard-foundation"):
        for path in sorted((ROOT / "packages" / package / "dist").glob("*.js")):
            data = path.read_bytes()
            outputs.append(
                {
                    "path": str(path.relative_to(ROOT)),
                    "bytes": len(data),
                    "gzip_bytes": len(gzip.compress(data, mtime=0)),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
    return {
        "schema_version": 2,
        "scope": "Installed distribution metadata and all committed lock profiles; declared licenses, not legal clearance",
        "project_distribution": "private_candidate_no_public_license_declared",
        "profiles": {
            path.name: [
                {"name": name, "version": version}
                for name, version in re.findall(
                    r"^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?==([^\s;]+)", path.read_text(), re.MULTILINE
                )
            ]
            for path in sorted(ROOT.glob("requirements-*.lock"))
        },
        "python": sorted(python, key=lambda entry: entry["name"].lower()),
        "node": sorted(node, key=lambda entry: entry["name"]),
        "compiled_javascript": outputs,
        "size_limit": "Per-file output only; excludes transitive dependencies and runtime",
        "locks": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in [
                *(p.name for p in sorted(ROOT.glob("requirements-*.lock"))),
                "package-lock.json",
            ]
        },
    }


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2))
