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
import subprocess
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
TRACE_SERVE = ROOT / "tools" / "context_trace_serve.py"
CAUSES = (
    "not_a_candidate",  # neither the semantic nor the keyword stage returned it
    "below_cut",  # a candidate, but fused rank puts it beyond the items a briefing may hold
    "folded",  # replaced by a near-identical or containing record in presentation
    "diversified",  # moved beyond the item limit by the near-identical cap
    "spread",  # moved beyond the item limit by spreading near-equal items
    "dropped_for_budget",  # chosen (or a neighbouring turn) but the packer left it out
    "excerpt_only",  # packed, but what is shown does not hold the start of the turn
    "shown_other",  # packed with its start and still not found (rendering)
)
RANK_BUCKETS = ((32, "1-32"), (64, "33-64"), (128, "65-128"), (256, "129-256"))
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


def window_share(turn: str, seen: str, size: int = 40) -> float:
    """Share of the turn's text (in windows of `size` characters) that is inside the context."""
    text = lme.norm(turn)
    if len(text) <= size:
        return 1.0 if text in seen else 0.0
    windows = [text[i : i + size] for i in range(0, len(text) - size + 1, size)]
    return sum(1 for w in windows if w in seen) / len(windows)


def bucket(rank: int) -> str:
    return next((name for top, name in RANK_BUCKETS if rank <= top), ">256")


def holder(rows: list[dict], size: int, stored: str) -> dict | None:
    """The recorded item that is this turn (same stored size, and its quoted start is in the turn)."""
    return next(
        (
            r
            for r in rows
            if r["single"]
            and r["source_bytes"] is not None
            and abs(r["source_bytes"] - size) <= 2
            and r["head"] in stored
        ),
        None,
    )


def diagnose(item: dict, context: str, trace: dict) -> list[dict]:
    """For every answer-bearing turn: whether `lme.evidence` counts it as shown and, when not,
    the first stage of the pipeline that lost it, read from the recorded lists of the pipeline
    itself (`tools/context_trace_serve.py`)."""
    seen = lme.norm(context)
    gold = set(item["answer_session_ids"])
    ranked = trace.get("ranked", [])
    folded = {r: i for i, r in enumerate(trace.get("folded", []), 1)}
    diversified = {r: i for i, r in enumerate(trace.get("diversified", []), 1)}
    spread = {r: i for i, r in enumerate(trace.get("spread", []), 1)}
    packed = {p["ref"]: p for p in trace.get("packed", [])}
    beside = trace.get("beside", [])
    need = trace.get("need", 0)
    out = []
    for sid, session in zip(item["haystack_session_ids"], item["haystack_sessions"], strict=True):
        if sid not in gold:
            continue
        for turn in session:
            if not turn.get("has_answer"):
                continue
            stored = lme.norm(f"{turn['role']}: {turn['content']}")
            size = len(f"{turn['role']}: {turn['content']}".encode())

            found = lme.norm(turn["content"])[: lme.PREFIX] in seen
            share = window_share(turn["content"], seen)
            record = {
                "bytes": size,
                "found": found,
                "window_share": round(share, 2),
                "whole_shown": share == 1.0,
                "cause": None,
            }
            entry = holder(ranked, size, stored)
            near = holder(beside, size, stored)
            if entry:
                record.update(
                    rank=entry["rank"],
                    rank_bucket=bucket(entry["rank"]),
                    in_lexical=entry["lex"] > 0,
                    in_dense=entry["has_dense"],
                )
            if not found:
                ref = (entry or near or {}).get("ref")
                if entry is None and near is None:
                    cause = "not_a_candidate"
                elif ref in packed:
                    shown_at = packed[ref]
                    cause = "excerpt_only" if (shown_at["start"] or 0) > 0 else "shown_other"
                elif near is not None and entry is None:
                    cause = "dropped_for_budget"
                elif ref not in folded:
                    cause = "folded"
                elif ref in spread and spread[ref] <= need:
                    cause = "dropped_for_budget"
                elif folded[ref] > need:
                    cause = "below_cut"
                elif diversified.get(ref, 0) > need:
                    cause = "diversified"
                else:
                    cause = "spread"
                record["cause"] = cause
                if entry:
                    record["cut_position"] = folded.get(ref)
            out.append(record)
    return out


def ask(item: dict, stores: Path, budget: int, trace: bool = False) -> dict:
    root = stores_path(stores, item)
    marker = root / "seeded"
    system = arm.create_semantic()
    system.ingest = "turns"
    system.prepare(root)
    dump = root / "trace.json"
    if trace:
        dump.unlink(missing_ok=True)
        original_popen = subprocess.Popen

        def traced(command, *args, **kwargs):
            if list(command[1:3]) == ["-m", "memoryking"]:
                command = [command[0], str(TRACE_SERVE), *command[3:]]
                kwargs["env"] = {**kwargs["env"], "CONTEXT_COVERAGE_TRACE": str(dump)}
            return original_popen(command, *args, **kwargs)

        subprocess.Popen = traced
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
        if trace:
            subprocess.Popen = original_popen
    recorded = json.loads(dump.read_text()) if trace else {}
    trace_list = diagnostics.get("trace") or []
    lexical = stage(trace_list, "lexical")
    dense, paging = stage(trace_list, "dense"), stage(trace_list, "paging")
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
        **({"gold": diagnose(item, context, recorded)} if trace else {}),
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


def misses(rows: list[dict]) -> dict:
    """Counts of the answer-bearing turns the measure does not find, by the stage that lost them,
    the fused rank of those that were candidates, and how far the measure can be trusted on
    turns shown as excerpts."""
    turns = [(r, g) for r in rows if r.get("gold") for g in r["gold"]]
    lost = [g for _, g in turns if not g["found"]]
    causes = {c: sum(1 for g in lost if g["cause"] == c) for c in CAUSES}
    ranks = {name: 0 for _, name in RANK_BUCKETS}
    ranks[">256"] = 0
    for g in lost:
        if "rank_bucket" in g:
            ranks[g["rank_bucket"]] += 1
    found = [g for _, g in turns if g["found"]]
    long = [g for _, g in turns if g["bytes"] > 4096]
    return {
        "answer_turns": len(turns),
        "found": len(found),
        "missed": len(lost),
        "questions_missing_a_turn": len({r["id"] for r, g in turns if not g["found"]}),
        "cause": causes,
        "missed_fused_rank": ranks,
        "missed_candidate_stage": {
            "lexical_and_dense": sum(1 for g in lost if g.get("in_lexical") and g.get("in_dense")),
            "lexical_only": sum(1 for g in lost if g.get("in_lexical") and not g.get("in_dense")),
            "dense_only": sum(
                1 for g in lost if "rank" in g and not g["in_lexical"] and g["in_dense"]
            ),
        },
        "measure": {
            "turns_over_4096_bytes": len(long),
            "found_whole": sum(1 for g in found if g["whole_shown"]),
            "found_by_start_only": sum(1 for g in found if not g["whole_shown"]),
            "found_with_under_half_shown": sum(1 for g in found if g["window_share"] < 0.5),
            "missed_but_part_shown": sum(1 for g in lost if g["window_share"] > 0),
            "missed_but_half_or_more_shown": sum(1 for g in lost if g["window_share"] >= 0.5),
        },
    }


def report(rows: list[dict], budget: int) -> dict:
    types = sorted({r["type"] for r in rows})
    traced = any(r.get("gold") for r in rows)
    return {
        "overall": summarize(rows, budget),
        "by_type": {t: summarize([r for r in rows if r["type"] == t], budget) for t in types},
        **(
            {
                "misses": misses(rows),
                "misses_by_type": {t: misses([r for r in rows if r["type"] == t]) for t in types},
            }
            if traced
            else {}
        ),
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
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="record the pipeline's own lists and report where each missed turn was lost",
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
                rows[item["question_id"]] = ask(item, args.stores, args.budget_chars, args.diagnose)
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
