"""Exercise owner Memory renewal in Chromium against a disposable SQLite dashboard."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("node", "chrome", "playwright", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mk-owner-browser-") as directory:
        demo = subprocess.Popen(
            [sys.executable, str(ROOT / "tools/dashboard_demo.py"), "--count", "3"],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert demo.stdout and demo.stdin
            setup = json.loads(demo.stdout.readline())
            options = Path(directory) / "options.json"
            options.write_text(
                json.dumps(
                    {
                        **setup,
                        "chrome": str(args.chrome),
                        "playwright": str(args.playwright),
                        "output": str(args.output.resolve()),
                    }
                )
            )
            options.chmod(0o600)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    str(args.node),
                    str(ROOT / "tools/dashboard-memory-browser-probe.mjs"),
                    str(options),
                ],
                cwd=ROOT,
                check=True,
                timeout=180,
            )
        finally:
            if demo.poll() is None:
                assert demo.stdin
                demo.stdin.write("stop\n")
                demo.stdin.flush()
                try:
                    demo.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    demo.kill()
                    demo.communicate()
    print("Owner Memory browser renewal and retention checks passed")


if __name__ == "__main__":
    os.umask(0o077)
    main()
