"""Inspect a built wheel and distributed browser/config files for auth fixture leakage."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKERS = (
    b"synthetic-private-secret",
    b"synthetic-provider-token",
    b"synthetic-verifier",
    b"controlled-code",
)
REQUIRED = {
    "memoryking/adapters/" + name + ".py"
    for name in (
        "github_oauth",
        "github_oauth_worker",
        "viewer_auth",
        "viewer_budget",
        "viewer_http",
    )
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    records = []
    with zipfile.ZipFile(args.wheel) as wheel:
        if not REQUIRED.issubset(set(wheel.namelist())):
            raise SystemExit("Wheel is missing viewer authentication modules")
        for name in sorted(wheel.namelist()):
            if not name.endswith("/"):
                raw = wheel.read(name)
                if any(marker in raw for marker in MARKERS):
                    raise SystemExit("Synthetic private marker present in distributed wheel")
                records.append(
                    {"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                )
    assets = []
    for directory in (
        ROOT / "src/memoryking/adapters/web",
        ROOT / "src/memoryking/adapters/viewer_web",
        ROOT / "packages/contracts/dist",
    ):
        if not directory.is_dir():
            raise SystemExit("Build distributed assets before the authentication audit")
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                raw = path.read_bytes()
                if any(marker in raw for marker in MARKERS):
                    raise SystemExit(
                        "Synthetic private marker present in distributed browser/contracts"
                    )
                assets.append(
                    {"path": str(path.relative_to(ROOT)), "sha256": hashlib.sha256(raw).hexdigest()}
                )
    config = json.loads((ROOT / "examples/viewer-config.json").read_text())
    from memoryking.adapters.viewer_config import validate_config

    validate_config(config)
    assert not config["enabled"]
    assert set(k for k in config if k.startswith("github_")) == {
        "github_client_id",
        "github_secret_file",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "passed": True,
                "scope": "Built package and distributed assets: presence of required adapters, absence of named synthetic private test markers, closed disabled example. Not a generic proof of no possible secrets.",
                "wheel_sha256": hashlib.sha256(args.wheel.read_bytes()).hexdigest(),
                "wheel_members": records,
                "browser_assets": assets,
                "config_example_sha256": hashlib.sha256(
                    (ROOT / "examples/viewer-config.json").read_bytes()
                ).hexdigest(),
            },
            indent=2,
        )
        + "\n"
    )
    print(
        f"Auth package audit passed: {len(records)} wheel members and {len(assets)} distributed assets"
    )


if __name__ == "__main__":
    main()
