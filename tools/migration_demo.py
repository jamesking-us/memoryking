"""Exercise the real migration CLI with synthetic data over local IPC or explicit shared HTTPS."""

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared-url")
    parser.add_argument("--credential", type=Path)
    parser.add_argument("--tenant")
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--project", default="migration-demo")
    args = parser.parse_args()
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"PYTHONPATH", "PYTHONHOME", "GMAIL_FROM", "GMAIL_APP_KEY", "KINDLE_TO"}
    }

    def run(*command):
        result = subprocess.run(
            [sys.executable, *command],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode:
            raise RuntimeError("Migration example command failed: " + result.stdout + result.stderr)
        return json.loads(result.stdout)

    with tempfile.TemporaryDirectory(prefix="mk-import-demo-", dir="/tmp") as temp:
        root = Path(temp)
        legacy = root / "legacy.sqlite"
        with sqlite3.connect(legacy) as db:
            db.executescript((ROOT / "fixtures/migration/kingforge-v1-schema.sql").read_text())
            db.execute(
                "INSERT INTO plans(id,title,objective,created_at,updated_at) VALUES(1,'Synthetic recovery','Preserve context',1720000000,1720000001)"
            )
            db.execute(
                "INSERT INTO steps(id,plan_id,seq,description,updated_at) VALUES(1,1,1,'Inspect replay',1720000001)"
            )
            db.execute(
                "INSERT INTO lessons(id,plan_id,title,situation,lesson,created_at) VALUES(1,1,'Retry identity','Crash','Reuse original source identity',1720000002)"
            )
        before = hashlib.sha256(legacy.read_bytes()).hexdigest()
        process = None
        try:
            if args.shared_url:
                if not args.credential or not args.tenant or not args.ca_file:
                    raise ValueError("Shared example requires credential, tenant and CA file")
                connection = [
                    "--url",
                    args.shared_url,
                    "--tenant",
                    args.tenant,
                    "--ca-file",
                    str(args.ca_file),
                    "--credential",
                    str(args.credential),
                    "--project",
                    args.project,
                ]
            else:
                data, control = root / "data", root / "control"
                run("-m", "memoryking", "init", "--store", str(data), "--control", str(control))
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "memoryking",
                        "serve",
                        "--store",
                        str(data),
                        "--control",
                        str(control),
                    ],
                    cwd=ROOT,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                assert json.loads(process.stdout.readline())["ready"]
                connection = [
                    "--socket",
                    str(data / "service.sock"),
                    "--credential",
                    str(control / "owner.credential"),
                    "--project",
                    args.project,
                ]
            bundle = root / "bundle"
            run(
                "-m",
                "memoryking.migration",
                "snapshot",
                "--source",
                str(legacy),
                "--directory",
                str(bundle),
            )
            common = ["--directory", str(bundle)]
            run(
                "-m",
                "memoryking.migration",
                "inspect",
                *common,
                "--source-id",
                "synthetic-town",
                "--profile",
                "kingforge-v1",
            )
            planned = run(
                "-m",
                "memoryking.migration",
                "plan",
                *common,
                "--source-id",
                "synthetic-town",
                "--profile",
                "kingforge-v1",
                *connection,
            )
            assert planned["planned_rows"] == 3 and planned["unresolved_count"] == 0
            first = run("-m", "memoryking.migration", "apply", *common, *connection, "--limit", "2")
            assert first["next_start"] == 2 and not first["end_reached"]
            last = run("-m", "memoryking.migration", "apply", *common, *connection, "--start", "2")
            assert last["end_reached"]
            mappings = {
                p.name: json.loads(p.read_text())["target"]
                for p in (bundle / "journal").glob("*.json")
                if p.name != "plan.json"
            }
            run("-m", "memoryking.migration", "apply", *common, *connection)
            repeated = {
                p.name: json.loads(p.read_text())["target"]
                for p in (bundle / "journal").glob("*.json")
                if p.name != "plan.json"
            }
            assert repeated == mappings
            verified = run("-m", "memoryking.migration", "reconcile", *common, *connection)
            assert verified["verified_rows"] == 3 and verified["verified_references"] == 2
            assert hashlib.sha256(legacy.read_bytes()).hexdigest() == before
            print(
                json.dumps(
                    {
                        "passed": True,
                        "backend": "shared HTTPS" if args.shared_url else "local IPC",
                        "verified_rows": 3,
                        "verified_references": 2,
                        "repeat_created_no_duplicates": True,
                        "source_unchanged": True,
                        "live_cutover": False,
                        "provider_calls": 0,
                    }
                )
            )
        finally:
            if process:
                process.terminate()
                process.communicate(timeout=10)


if __name__ == "__main__":
    main()
