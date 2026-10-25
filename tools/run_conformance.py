#!/usr/bin/env python3
"""Run real local scenarios where implemented; all other entries remain not evaluated."""

import argparse
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from local_conformance import SQLiteScenarios

from memoryking.application.conformance import run_scenario

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--backend", choices=["none", "sqlite", "shared"], default="sqlite")
    args = parser.parse_args()
    cases = [
        json.loads(path.read_text())
        for path in sorted((ROOT / "fixtures/scenarios").glob("*.json"))
    ]
    adapter = SQLiteScenarios() if args.backend == "sqlite" else None
    if args.backend == "shared":
        from shared_conformance import SharedScenarios

        adapter = SharedScenarios()
    try:
        results = [asdict(run_scenario(case, adapter)) for case in cases]
    finally:
        if adapter:
            adapter.close()
    report = {
        "backend": adapter.name if adapter else "none",
        "counts": dict(Counter(r["status"] for r in results)),
        "results": results,
        "product_suite_evidence": getattr(adapter, "evidence", []),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["counts"]))
    if any(r["status"] == "failed" for r in results):
        raise SystemExit(1)
    if args.require_complete and any(r["status"] != "passed" for r in results):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
