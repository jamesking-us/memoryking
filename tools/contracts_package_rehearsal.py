"""Pack and consume the private TypeScript contracts outside the workspace."""

import argparse
import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"GMAIL_FROM", "GMAIL_APP_KEY", "KINDLE_TO", "NODE_PATH"}
    }
    report = {"passed": False, "steps": [], "published": False}

    def run(name, command, cwd):
        result = subprocess.run(
            command, cwd=cwd, env=env, capture_output=True, text=True, timeout=180
        )
        (output / (name + ".log")).write_text(result.stdout + result.stderr)
        report["steps"].append({"name": name, "exit_code": result.returncode})
        if result.returncode:
            raise RuntimeError("Contract package rehearsal failed: " + name)
        print("PASS " + name, flush=True)
        return result.stdout

    try:
        packed = json.loads(
            run(
                "pack",
                ["npm", "pack", "--ignore-scripts", "--json", "--pack-destination", str(output)],
                ROOT / "packages/contracts",
            )
        )[0]
        archive = output / packed["filename"]
        report["artifact"] = packed
        report["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
        with tarfile.open(archive) as tar:
            names = tar.getnames()
            assert all(n == "package/package.json" or n.startswith("package/dist/") for n in names)
            for source in (ROOT / "packages/contracts").glob("*.schema.json"):
                stream = tar.extractfile("package/dist/" + source.name)
                assert stream is not None and stream.read() == source.read_bytes()
            stream = tar.extractfile("package/dist/schema.json")
            assert (
                stream is not None
                and stream.read() == (ROOT / "contracts/foundation-v1.schema.json").read_bytes()
            )
        # Resolve the standalone package, then require every external dependency
        # to match a version and integrity already in the workspace lock.
        with tempfile.TemporaryDirectory(prefix="mk-contract-consumer-", dir="/tmp") as temp:
            consumer = Path(temp)
            (consumer / "package.json").write_text(
                json.dumps({"name": "private-contract-consumer", "private": True, "type": "module"})
            )
            run(
                "resolve",
                [
                    "npm",
                    "install",
                    "--package-lock-only",
                    "--ignore-scripts",
                    "--no-audit",
                    "--no-fund",
                    str(archive),
                ],
                consumer,
            )
            locked = json.loads((ROOT / "package-lock.json").read_text())["packages"]
            approved = {
                (v.get("version"), v.get("integrity"))
                for k, v in locked.items()
                if "node_modules/" in k and v.get("integrity")
            }
            resolved = json.loads((consumer / "package-lock.json").read_text())
            for name, item in resolved["packages"].items():
                if not name or name == "node_modules/@memoryking/contracts":
                    continue
                assert (item["version"], item["integrity"]) in approved, name
            (output / "consumer-lock.json").write_text(json.dumps(resolved, indent=2) + "\n")
            run("install", ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], consumer)
            (consumer / "consumer.mjs").write_text(
                "import assert from 'node:assert/strict';\n"
                "import {canonicalBytes,loadStrict,validateRetrieval,validateExperience} from '@memoryking/contracts';\n"
                'assert.equal(canonicalBytes({z:2,a:1}).toString(),\'{"a":1,"z":2}\');\n'
                'assert.throws(()=>loadStrict(\'{"x":1,"x":2}\'));\n'
                "assert.throws(()=>validateRetrieval({}));\n"
                "assert.throws(()=>validateExperience({}));\n"
                "console.log('Installed package runtime schemas and canonicalization passed');\n"
            )
            run("consume", ["node", "consumer.mjs"], consumer)
            report["passed"] = True
    finally:
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
