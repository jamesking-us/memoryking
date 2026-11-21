"""Start a real local daemon, seed one observation and run the TypeScript client example."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    service = subprocess.Popen(
        [sys.executable, str(ROOT / "tools/client_test_service.py"), "local"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        description = json.loads(service.stdout.readline())
        environment = {
            **os.environ,
            "MK_PORT": str(description["port"]),
            "MK_INCARNATION": description["incarnation"],
            "MK_CREDENTIAL": description["credential_file"],
        }
        result = subprocess.run(
            ["node", "examples/client_typescript.mjs"], cwd=ROOT, env=environment, timeout=60
        )
    finally:
        service.stdin.close()
        service.wait(30)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
