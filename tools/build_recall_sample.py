"""Rebuild the committed evidence-recall sample from LongMemEval's oracle file.

`fixtures/evaluation/longmemeval-regression-sample.json` holds a fixed, stratified subset of
the questions of `longmemeval_oracle.json` (MIT licence, see NOTICE): the first N per
question type when ordered by the SHA-256 of the question id, abstention questions left out.
The oracle file holds only each question's evidence sessions, so the sample is a pooled
setting of a few hundred turns. Pass the downloaded file with `--data`; it is read as JSON
and nothing in it is executed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

PER_TYPE = 12


def select(data: list[dict], per_type: int) -> list[dict]:
    chosen: dict[str, list[dict]] = {}
    for item in sorted(data, key=lambda d: hashlib.sha256(d["question_id"].encode()).hexdigest()):
        if item["question_id"].endswith("_abs"):
            continue
        group = chosen.setdefault(item["question_type"], [])
        if len(group) < per_type and set(item["answer_session_ids"]) <= set(
            item["haystack_session_ids"]
        ):
            group.append(item)
    return [item for kind in sorted(chosen) for item in chosen[kind]]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--per-type", type=int, default=PER_TYPE)
    args = parser.parse_args()
    data = json.loads(args.data.read_text(encoding="utf-8"))
    items = select(data, args.per_type)
    args.output.write_text(
        json.dumps(items, separators=(",", ":"), ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",  # the committed sample's bytes, on every platform
    )
    print(f"{len(items)} questions, {args.output.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
