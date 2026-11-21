"""Pack and consume the TypeScript contracts and client outside the workspace.

The contracts stay private; the client is publishable but is only packed here, never published.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
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
            client(run, report, output, consumer, archive)
            report["passed"] = True
    finally:
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")


CLIENT_FILES = {"package/package.json", "package/README.md", "package/LICENSE", "package/NOTICE"}

CONSUMER_TS = """import { MemoryKing, MemoryKingError, MemoryKingLocal } from "@memoryking/client";
import type { Briefing } from "@memoryking/client";

declare const process: { env: Record<string, string | undefined> };
const local = new MemoryKingLocal({
  port: Number(process.env.MK_PORT),
  project: "demo",
  incarnation: String(process.env.MK_INCARNATION),
  credentialFile: String(process.env.MK_CREDENTIAL),
});
const briefing: Briefing = await local.recall("timeout deadline");
if (!briefing.text.includes("Raising every timeout")) throw new Error("recall missed the seed");
try {
  new MemoryKing({ url: "http://example.test", tenant: "t", project: "p", credential: "x" });
  throw new Error("plain http to a remote host was accepted");
} catch (error) {
  if (!(error instanceof MemoryKingError) || error.code !== "shared_tls_required") throw error;
}
console.log("Installed client package typed against its declarations and recalled from a daemon");
"""


def client(run, report, output, consumer, contracts_archive):
    """Pack the client, install it beside the packed contracts, type-check and run it."""
    packed = json.loads(
        run(
            "pack-client",
            ["npm", "pack", "--ignore-scripts", "--json", "--pack-destination", str(output)],
            ROOT / "packages/client",
        )
    )[0]
    archive = output / packed["filename"]
    report["client"] = {
        "artifact": packed,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }
    with tarfile.open(archive) as tar:
        for name in tar.getnames():
            assert name in CLIENT_FILES or (
                name.startswith("package/dist/") and name.endswith((".js", ".d.ts"))
            ), name
        manifest = json.loads(tar.extractfile("package/package.json").read())
    # Zero runtime dependencies is a promise to consumers, so the packed manifest proves it.
    assert not manifest.get("dependencies") and not manifest.get("peerDependencies"), manifest
    assert manifest.get("license") == "Apache-2.0" and "private" not in manifest
    run(
        "install-client",
        ["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund", str(archive)],
        consumer,
    )
    resolved = json.loads((consumer / "package-lock.json").read_text())
    assert resolved["packages"]["node_modules/@memoryking/client"].get("dependencies") is None
    (consumer / "consumer-client.mts").write_text(CONSUMER_TS)
    (consumer / "tsconfig.json").write_text(
        json.dumps(
            {
                "compilerOptions": {
                    "target": "ES2022",
                    "module": "NodeNext",
                    "moduleResolution": "NodeNext",
                    "strict": True,
                    "noEmit": True,
                    "skipLibCheck": False,
                    "types": [],
                },
                "files": ["consumer-client.mts"],
            }
        )
    )
    run("typecheck-client", [str(ROOT / "node_modules/.bin/tsc"), "-p", "."], consumer)
    service = subprocess.Popen(
        [sys.executable, str(ROOT / "tools/client_test_service.py"), "local"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        description = json.loads(service.stdout.readline())
        (consumer / "run-client.mjs").write_text(
            CONSUMER_TS.replace("const briefing: Briefing", "const briefing")
            .replace("declare const process: { env: Record<string, string | undefined> };\n", "")
            .replace('import type { Briefing } from "@memoryking/client";\n', "")
        )
        environment = {
            **os.environ,
            "MK_PORT": str(description["port"]),
            "MK_INCARNATION": description["incarnation"],
            "MK_CREDENTIAL": description["credential_file"],
        }
        result = subprocess.run(
            ["node", "run-client.mjs"],
            cwd=consumer,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        (output / "consume-client.log").write_text(result.stdout + result.stderr)
        report["steps"].append({"name": "consume-client", "exit_code": result.returncode})
        if result.returncode:
            raise RuntimeError("Contract package rehearsal failed: consume-client")
        print("PASS consume-client", flush=True)
    finally:
        service.stdin.close()
        service.wait(30)


if __name__ == "__main__":
    main()
