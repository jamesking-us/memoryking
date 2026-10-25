#!/usr/bin/env python3
"""Check product documentation links and execute the maintained examples."""

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    failures = []
    paths = [
        ROOT / "README.md",
        ROOT / "AGENTS.md",
        *ROOT.glob("docs/*.md"),
        ROOT / "CONTRIBUTING.md",
        ROOT / "SECURITY.md",
    ]
    for path in paths:
        for target in re.findall(r"\]\(([^)]+)\)", path.read_text()):
            if target.startswith(("https:", "http:", "#", "mailto:")):
                continue
            if not (path.parent / target.split("#")[0]).exists():
                failures.append(f"{path.name}: missing {target}")
    examples = json.loads((ROOT / "docs/developer-examples.json").read_text())
    for example in examples:
        command = [sys.executable if arg == "{python}" else arg for arg in example["command"]]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        if result.returncode != example["exit_code"] or example["contains"] not in result.stdout:
            failures.append("Example failed: " + example["id"])
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"Checked {len(paths)} documents and {len(examples)} executable examples")


if __name__ == "__main__":
    main()
