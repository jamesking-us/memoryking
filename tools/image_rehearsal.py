#!/usr/bin/env python3
"""Build the local-daemon container image and prove it serves; record its digest for signing.

Runs on a host with a Docker engine (not inside the development container). Nothing is
pushed. The recorded digest is what the owner signs and publishes once a licence exists.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def docker(*args: str, check: bool = True, timeout: int = 600) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, cwd=ROOT
    )
    if check and result.returncode:
        raise SystemExit(f"docker {' '.join(args)} failed:\n{result.stdout}{result.stderr}")
    return result.stdout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="memoryking:local")
    parser.add_argument("--output", type=Path, required=True, help="New JSON report path")
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("choose a new --output path")
    started = time.monotonic()
    docker("build", "--pull=false", "-f", "deploy/local/Dockerfile", "-t", args.tag, ".")
    image_id = docker("image", "inspect", "--format", "{{.Id}}", args.tag).strip()
    size = int(docker("image", "inspect", "--format", "{{.Size}}", args.tag).strip())
    stamp = str(int(time.time()))
    name = "mk-image-rehearsal-" + stamp
    volumes = [f"mk-rehearsal-data-{stamp}", f"mk-rehearsal-control-{stamp}"]
    # Named volumes take the image's ownership of /data and /control, as a deployment would.
    for volume in volumes:
        docker("volume", "create", volume)
    mounts = ["-v", f"{volumes[0]}:/data", "-v", f"{volumes[1]}:/control"]
    try:
        init = docker(
            "run",
            "--rm",
            *mounts,
            args.tag,
            "init",
            "--store",
            "/data/store",
            "--control",
            "/control/store",
        )
        credential = json.loads(init)["credential_file"]
        docker("run", "-d", "--name", name, *mounts, args.tag)
        try:
            ready = None
            for _ in range(60):
                logs = docker("logs", name, check=False)
                if '"ready": true' in logs:
                    ready = json.loads(logs.strip().splitlines()[-1])
                    break
                time.sleep(0.5)
            if ready is None:
                raise SystemExit(
                    "daemon did not report ready:\n" + docker("logs", name, check=False)
                )
            status = docker(
                "exec",
                name,
                "python",
                "-m",
                "memoryking",
                "status",
                "--socket",
                ready["socket"],
                "--credential",
                credential,
                "--json",
            )
            version = docker("exec", name, "python", "-m", "memoryking", "--version").strip()
            inside = docker("exec", name, "id", "-u").strip()
        finally:
            docker("rm", "-f", name, check=False)
    finally:
        for volume in volumes:
            docker("volume", "rm", "-f", volume, check=False)
    report = {
        "tag": args.tag,
        "image_id": image_id,
        "size_bytes": size,
        "version": version,
        "ran_as_uid": inside,
        "status": json.loads(status),
        "seconds": round(time.monotonic() - started, 1),
        "pushed": False,
        "sign": f"cosign sign --key <owner-key> {args.tag}@{image_id}  # after a licence is chosen",
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "status"}, indent=2))


if __name__ == "__main__":
    main()
