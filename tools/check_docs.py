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
        for target in re.findall(r"\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            if target.startswith(("https:", "http:", "#", "mailto:")):
                continue
            if not (path.parent / target.split("#")[0]).exists():
                failures.append(f"{path.name}: missing {target}")
    # The changelog's text is also the release page and the PyPI page: links must be absolute.
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    for target in re.findall(r"\]\(([^)]+)\)", changelog):
        if not target.startswith(("https:", "http:", "mailto:")):
            failures.append(f"CHANGELOG.md: relative link {target}")
    examples = json.loads((ROOT / "docs/developer-examples.json").read_text(encoding="utf-8"))
    not_here = []
    for example in examples:
        if sys.platform == "win32" and example.get("needs_store"):
            not_here.append(example["id"])  # the store runs in WSL2 or the container there
            continue
        command = [sys.executable if arg == "{python}" else arg for arg in example["command"]]
        result = subprocess.run(
            command, cwd=ROOT, capture_output=True, encoding="utf-8", errors="replace"
        )
        if result.returncode != example["exit_code"] or example["contains"] not in result.stdout:
            failures.append("Example failed: " + example["id"])
    if failures:
        raise SystemExit("\n".join(failures))
    ran = len(examples) - len(not_here)
    print(f"Checked {len(paths)} documents and {ran} executable examples")
    if not_here:
        print(
            f"Not applicable on Windows ({len(not_here)}, they need the memory store): "
            + ", ".join(not_here)
        )


if __name__ == "__main__":
    main()
