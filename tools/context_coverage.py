"""Judge-free coverage of the context a question gets, on development draws of LongMemEval `_s`.

This is retrieval evidence, not answer quality: it asks whether the text of the answer-bearing
turns is inside the context a reader would be shown. That is necessary for a right answer and
not sufficient (the reader may still fail to use it). No model is called: no reader, no
judge, no provider.

For each question of a draw the haystack (about 50 sessions) is recorded turn by turn into its
own store through the `memoryking_semantic` arm of `benchmarks/longhaul` (every turn one
observation at its session's date, the daemon indexing by meaning with the installed model),
and the context is the arm's: a text briefing for the question under the character budget
(default 12,000 characters). Per question type and overall it reports:

* `all`: the share of questions whose context holds every answer-bearing turn;
* `any`: the share whose context holds at least one;
* `chars`: characters of the budget the context uses;
* `candidates`: how many records the semantic and keyword stages considered, and the share of
  questions where a stage hit its candidate limit;
* `sessions`: how many distinct sessions the shown turns come from.

Only development draws are allowed (seeds 1 to 5); the published draw and the reserved ones are
refused. A question that appears in several draws is counted once in the pooled figures.

Seeding a store is the slow part (about 500 records a question). Stores are kept under
`--stores` (a short path outside the repository: a Unix socket path must fit in 100 bytes), so
a second run of the same draw only starts the daemon and asks. A kept store holds the benchmark
text and must never be committed. The data file is untrusted third-party text: it is read as
JSON and nothing in it is executed.

    python tools/context_coverage.py --seed 1 --seed 2 --stores /tmp/mk-ptwo --output out.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "benchmarks"))

from longhaul import lme  # noqa: E402
from longhaul.systems import memoryking as arm  # noqa: E402

from memoryking import hooks  # noqa: E402

DEVELOPMENT_SEEDS = (1, 2, 3, 4, 5)
SIZE = 60
NOTE = (
    "retrieval evidence only: whether the text of the answer-bearing turns is in the context "
    "shown; necessary for a right answer, not sufficient; no model was called"
)


def stores_path(stores: Path, item: dict) -> Path:
    return stores / hashlib.sha256(item["question_id"].encode()).hexdigest()[:10]


class Capture:
    """Remember the last packet `hooks.recall` returned, so the receipt can be read back."""

    def __init__(self) -> None:
        self.packet: dict = {}
        self.original = hooks.recall

    def __enter__(self):
        def recall(*args, **kwargs):
            self.packet = self.original(*args, **kwargs)
            return self.packet

        hooks.recall = recall
        return self

    def __exit__(self, *exc) -> None:
        hooks.recall = self.original


def sessions_shown(item: dict, context: str) -> int:
    """Distinct haystack sessions that have a turn in the context (a turn counts when its
    first characters are there; very short turns are ignored, they match anywhere)."""
    seen = lme.norm(context)
    count = 0
    for session in item["haystack_sessions"]:
        for turn in session:
            text = lme.norm(turn["content"])
            if len(text) >= 24 and text[: lme.PREFIX] in seen:
                count += 1
                break
    return count


def stage(trace: list[dict], name: str) -> dict:
    return next((t for t in trace if t.get("stage") == name), {})


def ask(item: dict, stores: Path, budget: int) -> dict:
    root = stores_path(stores, item)
    marker = root / "seeded"
    system = arm.create_semantic()
    system.ingest = "turns"
    system.prepare(root)
    started = time.perf_counter()
    seeded = marker.exists()
    try:
        if not seeded:
            system.seed(lme.sessions_of(item))
        system.before_retrieval(lme.SCOPE)
        marker.write_text("1")
        index_seconds = time.perf_counter() - started
        with Capture() as capture:
            context = system.context_for(item["question"], lme.SCOPE, budget)
        cred = system._credential(lme.SCOPE)
        diagnostics = system._receipt_diagnostics(capture.packet, lme.SCOPE, cred)
    finally:
        system.close()
    trace = diagnostics.get("trace") or []
    lexical, dense, paging = stage(trace, "lexical"), stage(trace, "dense"), stage(trace, "paging")
    found = lme.evidence(item, context)
    return {
        "id": item["question_id"],
        "type": item["question_type"],
        "answer_turns": found["answer_turns"],
        "all": found["all_found"],
        "any": found["any_found"],
        "chars": len(context),
        "sessions": sessions_shown(item, context),
        "dense_candidates": dense.get("visible_candidates"),
        "dense_limit_reached": bool(dense.get("candidate_limit_reached")),
        "lexical_candidates": lexical.get("visible_candidates"),
        "matched": paging.get("matched"),
        "items": len(diagnostics.get("selected") or []),
        "seeded_now": not seeded,
        "index_seconds": round(index_seconds, 1),
    }


def mean(values) -> float | None:
    values = [v for v in values if v is not None]
    return round(statistics.fmean(values), 3) if values else None


def summarize(rows: list[dict], budget: int) -> dict:
    scored = [r for r in rows if r["answer_turns"] > 0]  # abstention questions have no gold
    return {
        "questions": len(rows),
        "scored": len(scored),
        "all": mean(1.0 if r["all"] else 0.0 for r in scored),
        "any": mean(1.0 if r["any"] else 0.0 for r in scored),
        "chars_mean": mean(r["chars"] for r in rows),
        "budget_used": mean(r["chars"] / budget for r in rows),
        "items_mean": mean(r["items"] for r in rows),
        "dense_candidates_mean": mean(r["dense_candidates"] for r in rows),
        "dense_limit_reached": mean(1.0 if r["dense_limit_reached"] else 0.0 for r in rows),
        "lexical_candidates_mean": mean(r["lexical_candidates"] for r in rows),
        "matched_mean": mean(r["matched"] for r in rows),
        "sessions_mean": mean(r["sessions"] for r in rows),
    }


def report(rows: list[dict], budget: int) -> dict:
    types = sorted({r["type"] for r in rows})
    return {
        "overall": summarize(rows, budget),
        "by_type": {t: summarize([r for r in rows if r["type"] == t], budget) for t in types},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=lme.DEFAULT_DATA)
    parser.add_argument("--seed", type=int, action="append", help="development seed 1 to 5")
    parser.add_argument("--size", type=int, default=SIZE)
    parser.add_argument("--budget-chars", type=int, default=12000)
    parser.add_argument("--stores", type=Path, default=Path("/tmp/mk-ptwo"))
    parser.add_argument("--limit", type=int, help="only the first N questions of each draw")
    parser.add_argument(
        "--cached-only", action="store_true", help="skip questions whose store is not built yet"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    seeds = args.seed or [1]
    bad = [s for s in seeds if s not in DEVELOPMENT_SEEDS]
    if bad:
        parser.error(f"only development seeds {DEVELOPMENT_SEEDS} are allowed, not {bad}")
    data, digest = lme.load(args.data)
    args.stores.mkdir(parents=True, exist_ok=True)
    rows: dict[str, dict] = {}
    draws: dict[str, list[str]] = {}
    started = time.perf_counter()
    for seed in seeds:
        chosen = lme.select(data, args.size, seed)[: args.limit]
        draws[str(seed)] = []
        for number, item in enumerate(chosen, 1):
            if args.cached_only and not (stores_path(args.stores, item) / "seeded").exists():
                continue
            draws[str(seed)].append(item["question_id"])
            if item["question_id"] not in rows:
                rows[item["question_id"]] = ask(item, args.stores, args.budget_chars)
            print(
                f"seed {seed} {number}/{len(chosen)} {item['question_type']} "
                f"all={rows[item['question_id']]['all']}",
                file=sys.stderr,
                flush=True,
            )
    result = {
        "note": NOTE,
        "data_sha256": digest,
        "seeds": seeds,
        "budget_chars": args.budget_chars,
        "distinct_questions": len(rows),
        "questions_in_draws": sum(len(v) for v in draws.values()),
        "pooled_distinct": report(list(rows.values()), args.budget_chars),
        "per_draw": {
            s: report([rows[q] for q in ids], args.budget_chars) for s, ids in draws.items()
        },
        "seconds": round(time.perf_counter() - started, 1),
        "rows": sorted(rows.values(), key=lambda r: r["id"]),
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(NOTE)
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
