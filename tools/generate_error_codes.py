"""Write docs/error-codes.md from the hint table; `--check` reports drift."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memoryking.domain.hints import HINTS, hint  # noqa: E402


def raised_codes() -> set[str]:
    codes: set[str] = set()
    for path in (ROOT / "src/memoryking").rglob("*.py"):
        codes.update(re.findall(r'LocalError\("([a-z0-9_]+)"', path.read_text()))
    codes.update({"store_required", "control_required", "socket_required", "credential_required"})
    return codes


def render() -> str:
    codes = sorted(set(HINTS) | raised_codes())
    lines = [
        "# Error codes",
        "",
        "Every error reply carries `error` and a one-line `hint`. This page is generated from",
        "the same table (`memoryking.domain.hints`) by `tools/generate_error_codes.py`, so it",
        "cannot drift from what the service says. Codes are grouped by their first word.",
        "",
    ]
    group = None
    for code in codes:
        prefix = code.split("_")[0]
        if prefix != group:
            group = prefix
            lines.extend([f"## {group}", "", "| Code | What to do |", "| --- | --- |"])
        lines.append(f"| `{code}` | {hint(code)} |")
        if (
            codes.index(code) + 1 < len(codes)
            and codes[codes.index(code) + 1].split("_")[0] != group
        ):
            lines.append("")
    lines.extend(["", "[Back to the reference](reference.md#troubleshooting)", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = ROOT / "docs/error-codes.md"
    text = render()
    if args.check:
        if not target.exists() or target.read_text() != text:
            print("error-codes.md drift; run tools/generate_error_codes.py")
            return 1
        print("error-codes.md matches")
        return 0
    target.write_text(text)
    print(f"wrote {target.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
