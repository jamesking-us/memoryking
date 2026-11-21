"""Judge-free evidence recall on LongMemEval-format data, through real briefings.

LongMemEval labels, for every question, which chat sessions hold the evidence and, per
turn, whether that turn carries the answer. That makes retrieval measurable without a
model judging anything: capture the sessions as observations, ask for a briefing with
the question, and count whether the cited observations come from the evidence sessions
(and turns). No answer is generated, so no LLM-as-judge is involved; this measures the
memory layer alone, which is also why the numbers are not comparable to the leaderboard's
answer-accuracy figures.

Data: pass `--data` with `longmemeval_oracle.json`, `longmemeval_s_cleaned.json` or any
file in that format (download separately; the full files are not committed). Two settings:

* pooled (default): every question's sessions go into one project, so the oracle file's
  evidence sessions (about 950 of them) compete against each other for every question.
* per question (`--no-pool`): each question is asked against a fresh store holding only its
  own haystack. This is the setting other systems publish for `longmemeval_s` (about 50
  sessions per question).

`--as-of-question-date` (default on) briefs as of the question's date. `--per-type N` keeps
the first N questions of each type (a stratified subset); `--questions N` the first N.

Metrics (session level: the ranking collapsed to distinct sessions; turn level: the cited
observations, each one chat turn, against the answer turns of the evidence sessions):
recall_any@K, recall_all@K for K = 5 and 10, and mean reciprocal rank. See the report's
`protocol` field and docs/reference.md for the exact definitions.

`--gate` compares the report with `fixtures/evaluation/evidence-recall-gate.json`; the
check runs it on the committed sample so the suite stays offline.

`--compare BASELINE.json` judges a change against an earlier report of the same data and
setting: it prints every metric and every question type's delta and exits 1 when a metric
falls by more than `--tolerance` (a question type's by more than `--type-tolerance`).
Pass `--current REPORT.json` to compare two saved reports without measuring again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memoryking.adapters.local_store import LocalStore  # noqa: E402
from memoryking.adapters.retrieval import Retrieval  # noqa: E402

GATE = ROOT / "fixtures/evaluation/evidence-recall-gate.json"
TEXT_LIMIT = 60000
KS = (5, 10)
BUDGET = 65536
DEPTH = 32  # the contract's largest top_k: deep enough for ten distinct sessions
LEVELS = ("session", "turn")
HEADLINE = (
    [f"{level}_recall_{kind}_at_{k}" for level in LEVELS for kind in ("any", "all") for k in KS]
    + ["session_mrr_distinct", "turn_mrr"]
    + ["session_recall_at_k", "answer_turn_recall_at_k", "session_mrr"]
)
TYPE_METRICS = (
    "session_recall_any_at_5",
    "session_recall_any_at_10",
    "turn_recall_any_at_5",
    "turn_recall_any_at_10",
    "session_mrr_distinct",
    "turn_mrr",
    "session_recall_at_k",
)
PROTOCOL = (
    "judge_free_evidence_recall_v2: every haystack turn is recorded as one observation at its "
    "session's date; each question is asked through a real briefing (as of its date) for "
    f"{DEPTH} results under a token budget large enough not to cut the list; the cited observations, in ranked order, are scored against the labelled "
    "evidence sessions and answer turns. Session level collapses the ranking to distinct "
    "sessions in order of first appearance; turn level scores the observations themselves "
    "against the has_answer turns of the question's own evidence sessions. recall_any@K is 1 "
    "when any gold item is in the top K and recall_all@K when every one is; MRR is the "
    "reciprocal rank of the first gold item (0 when none is returned). The legacy "
    "session_recall_at_k, answer_turn_recall_at_k and session_mrr use the first top_k "
    "observations, and session_mrr counts observation positions. No answer is generated or judged"
)


def session_ms(stamp: str) -> int:
    """`2023/05/20 (Sat) 02:21` or an ISO date-time, as Unix ms (UTC)."""
    text = stamp.strip()
    for fmt in ("%Y/%m/%d (%a) %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {stamp!r}")


def load(
    path: Path,
    limit: int | None,
    types: set[str] | None,
    per_type: int | None = None,
) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise ValueError("expected a list of question instances")
    items = [d for d in data if not types or d["question_type"] in types]
    if per_type:
        taken: dict[str, int] = {}
        kept = []
        for item in items:
            if taken.get(item["question_type"], 0) < per_type:
                taken[item["question_type"]] = taken.get(item["question_type"], 0) + 1
                kept.append(item)
        items = kept
    return items[:limit] if limit else items


class StampClock:
    """A clock the harness shifts by hand, so records carry the session's date.

    Wall time is the real clock plus an offset that only grows (the store refuses a
    step back), and the monotonic clock is the real one, so both keep ticking together
    and release deadlines behave as in production."""

    def __init__(self, utc_ms: int):
        self.offset = utc_ms - time.time_ns() // 1_000_000

    def utc_ms(self) -> int:
        return time.time_ns() // 1_000_000 + self.offset

    def monotonic_ms(self) -> int:
        return time.monotonic_ns() // 1_000_000

    def advance_to(self, utc_ms: int) -> None:
        self.offset = max(self.offset, utc_ms - time.time_ns() // 1_000_000)


def distinct(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def rank_of(ranked: list[str], gold: set[str]) -> int | None:
    return next((n for n, x in enumerate(ranked, 1) if x in gold), None)


def scores(ranked: list[str], gold: set[str]) -> dict[str, float]:
    """Hit-any, hit-all and reciprocal rank of one ranked list against its gold set."""
    out: dict[str, float] = {}
    for k in KS:
        top = set(ranked[:k])
        out[f"any_at_{k}"] = 1.0 if gold & top else 0.0
        out[f"all_at_{k}"] = 1.0 if gold and gold <= top else 0.0
    rank = rank_of(ranked, gold)
    out["mrr"] = 1 / rank if rank else 0.0
    return out


def run(
    questions: list[dict[str, Any]],
    *,
    top_k: int,
    pool: bool,
    as_of: bool,
    budget: int,
    reranker: Any | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Pooled: one store and project for every question's sessions. Per question
    (`pool=False`): a fresh store holding only that question's own haystack."""
    groups = [questions] if pool else [[item] for item in questions]
    rows: list[dict[str, Any]] = []
    observations = 0
    capture_seconds = 0.0
    latencies: list[float] = []
    tokens: list[int] = []
    for number, group in enumerate(groups, 1):
        found = run_group(group, top_k=top_k, as_of=as_of, budget=budget, reranker=reranker)
        rows += found["rows"]
        observations += found["observations"]
        capture_seconds += found["capture_seconds"]
        latencies += found["latencies"]
        tokens += found["tokens"]
        if progress:
            progress(number, len(groups))

    def mean(values: list[float]) -> float | None:
        return round(statistics.fmean(values), 4) if values else None

    report: dict[str, Any] = {
        "questions": len(questions),
        "observations": observations,
        "pooled": pool,
        "as_of_question_date": as_of,
        "top_k": top_k,
        "depth": DEPTH,
        "budget": budget,
        "reranker": getattr(reranker, "fingerprint", None),
        # Legacy figures over the first `top_k` cited observations (see the protocol).
        "session_recall_at_k": mean([r["hit_k"] for r in rows]),
        "answer_turn_recall_at_k": mean([r["turn_hit_k"] for r in rows]),
        "session_mrr": mean([r["legacy_rr"] for r in rows]),
    }
    for level in LEVELS:
        for k in KS:
            for kind in ("any", "all"):
                report[f"{level}_recall_{kind}_at_{k}"] = mean(
                    [r[level][f"{kind}_at_{k}"] for r in rows]
                )
    report["session_mrr_distinct"] = mean([r["session"]["mrr"] for r in rows])
    report["turn_mrr"] = mean([r["turn"]["mrr"] for r in rows])
    by_type: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_type.setdefault(row["type"], []).append(row)
    report["by_question_type"] = {
        kind: {
            "questions": len(group),
            "session_recall_at_k": mean([r["hit_k"] for r in group]),
            "session_recall_any_at_5": mean([r["session"]["any_at_5"] for r in group]),
            "session_recall_any_at_10": mean([r["session"]["any_at_10"] for r in group]),
            "turn_recall_any_at_5": mean([r["turn"]["any_at_5"] for r in group]),
            "turn_recall_any_at_10": mean([r["turn"]["any_at_10"] for r in group]),
            "session_mrr_distinct": mean([r["session"]["mrr"] for r in group]),
            "turn_mrr": mean([r["turn"]["mrr"] for r in group]),
        }
        for kind, group in sorted(by_type.items())
    }
    report["per_question"] = {
        r["id"]: {
            "type": r["type"],
            "session_rank": r["session_rank"],
            "turn_rank": r["turn_rank"],
        }
        for r in rows
    }
    report.update(
        tokens_mean=round(statistics.fmean(tokens), 1) if tokens else None,
        latency_ms_p50=round(statistics.median(latencies), 2) if latencies else None,
        latency_ms_p95=round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 2)
        if latencies
        else None,
        capture_seconds=round(capture_seconds, 2),
        protocol=PROTOCOL,
    )
    return report


def run_group(
    questions: list[dict[str, Any]],
    *,
    top_k: int,
    as_of: bool,
    budget: int,
    reranker: Any | None,
) -> dict[str, Any]:
    """One store. Several questions share it only in the pooled setting, where they also
    share one project; a single question's project is its own either way."""
    project = "lme"
    # Every session once, oldest first, so the store records it at the session's date.
    sessions: dict[str, tuple[int, list[dict[str, Any]]]] = {}
    for item in questions:
        for sid, date, turns in zip(
            item["haystack_session_ids"],
            item["haystack_dates"],
            item["haystack_sessions"],
            strict=True,
        ):
            sessions.setdefault(sid, (session_ms(date), turns))
    ordered = sorted(sessions.items(), key=lambda entry: (entry[1][0], entry[0]))
    first_ms = ordered[0][1][0] if ordered else int(time.time() * 1000)
    clock = StampClock(first_ms - 1)
    with tempfile.TemporaryDirectory(prefix="mk-evidence-", dir="/tmp") as directory:
        root = Path(directory)
        credential = LocalStore.initialize(root / "db", root / "ctl").read_text()
        with LocalStore(root / "db", root / "ctl", clock=clock) as store:
            store.reranker = reranker
            retrieval = Retrieval(store)
            captured: dict[str, tuple[str, bool]] = {}  # observation -> (session, has_answer)
            answer_turns: dict[str, set[str]] = {}  # session -> its answer observations
            namespace = store.namespace(credential, project)
            started_capture = time.perf_counter()
            for sid, (when, turns) in ordered:
                clock.advance_to(when)
                safe = re.sub(r"[^A-Za-z0-9_-]", "-", sid)[:80]
                for index, turn in enumerate(turns):
                    text = f"{turn['role']}: {turn['content']}"[:TEXT_LIMIT]
                    if not text.strip():
                        continue
                    result = store.capture(
                        credential,
                        namespace,
                        f"{safe}-{index}",
                        {
                            "version": 1,
                            "project": project,
                            "source": "longmemeval",
                            "source_event": f"{safe}-{index}",
                            "text": text,
                            "readers": None,
                            "classification": "internal",
                            "observed_at_ms": when + index,
                        },
                    )
                    answer = bool(turn.get("has_answer"))
                    captured[result["observation"]] = (sid, answer)
                    if answer:
                        answer_turns.setdefault(sid, set()).add(result["observation"])
                    # The daemon drains audit jobs between requests; the harness must too.
                    if len(captured) % 200 == 0:
                        while store.drain():
                            pass
            while store.drain():
                pass
            capture_seconds = time.perf_counter() - started_capture

            rows: list[dict[str, Any]] = []
            tokens: list[int] = []
            latencies: list[float] = []
            # Questions are asked in date order so the clock never steps back.
            asked = sorted(
                questions,
                key=lambda item: (
                    session_ms(item["question_date"]) if item.get("question_date") else 0
                ),
            )
            for item in asked:
                when = session_ms(item["question_date"]) if item.get("question_date") else None
                if when is not None:
                    clock.advance_to(when)
                evidence = set(item["answer_session_ids"])
                gold_turns: set[str] = set()
                for sid in evidence:
                    gold_turns |= answer_turns.get(sid, set())
                args: dict[str, Any] = {
                    "project": project,
                    "query": item["question"][:2048],
                    "bindings": [{}],
                    "max_output_tokens": budget,
                    "tokenizer": "utf8-byte-estimate-v1",
                    "top_k": max(top_k, DEPTH),
                }
                if as_of and when is not None:
                    args["as_of_ms"] = when
                if reranker is not None:
                    args["rerank"] = "on"
                command = {
                    "version": 1,
                    "incarnation": store.incarnation,
                    "action": "brief",
                    "args": args,
                }
                started = time.perf_counter()
                emitted: list[bytes] = []
                retrieval.execute(credential, command).emit(emitted.append)
                latencies.append((time.perf_counter() - started) * 1000)
                packet = json.loads(emitted[0])
                cited = [g["reference"] for g in packet["guidance"] if g["reference"] in captured]
                ranked_sessions = distinct([captured[c][0] for c in cited])
                head = cited[:top_k]
                head_sessions = [captured[c][0] for c in head]
                first = next((n for n, s in enumerate(head_sessions, 1) if s in evidence), None)
                session = scores(ranked_sessions, evidence)
                turn = scores(cited, gold_turns) if gold_turns else dict.fromkeys(session, 0.0)
                rows.append(
                    {
                        "id": item["question_id"],
                        "type": item["question_type"],
                        "session": session,
                        "turn": turn,
                        "hit_k": 1.0 if evidence & set(head_sessions) else 0.0,
                        "turn_hit_k": 1.0 if gold_turns & set(head) else 0.0,
                        "legacy_rr": 1 / first if first else 0.0,
                        "session_rank": rank_of(ranked_sessions, evidence),
                        "turn_rank": rank_of(cited, gold_turns),
                    }
                )
                tokens.append(packet["usage"]["output_tokens"])
    return {
        "rows": rows,
        "observations": len(captured),
        "capture_seconds": capture_seconds,
        "latencies": latencies,
        "tokens": tokens,
    }


def gate(report: dict[str, Any], thresholds: dict[str, Any]) -> list[str]:
    failures = []
    for metric, bound in thresholds.get("minimum", {}).items():
        if report.get(metric) is None or report[metric] < bound:
            failures.append(f"{metric} {report.get(metric)} below {bound}")
    for metric, bound in thresholds.get("maximum", {}).items():
        if report.get(metric) is None or report[metric] > bound:
            failures.append(f"{metric} {report.get(metric)} above {bound}")
    for metric, bounds in thresholds.get("by_question_type_minimum", {}).items():
        for kind, bound in bounds.items():
            value = report.get("by_question_type", {}).get(kind, {}).get(metric)
            if value is None or value < bound:
                failures.append(f"{kind} {metric} {value} below {bound}")
    return failures


def compare(
    baseline: dict[str, Any],
    current: dict[str, Any],
    *,
    tolerance: float,
    type_tolerance: float,
) -> tuple[list[str], list[str]]:
    """(printable lines, regressions). A regression is a drop beyond the tolerance."""
    lines: list[str] = []
    regressions: list[str] = []

    def row(name: str, before: Any, after: Any, limit: float) -> None:
        if before is None or after is None:
            lines.append(f"{name:<44} {before!s:>8} {after!s:>8}")
            return
        delta = after - before
        flag = ""
        if delta < -limit - 1e-9:
            flag = "  REGRESSION"
            regressions.append(f"{name} {before} -> {after} (limit {limit})")
        lines.append(f"{name:<44} {before:>8.4f} {after:>8.4f} {delta:>+8.4f}{flag}")

    lines.append(f"{'metric':<44} {'before':>8} {'after':>8} {'delta':>8}")
    for metric in HEADLINE:
        row(metric, baseline.get(metric), current.get(metric), tolerance)
    for kind in sorted(set(baseline["by_question_type"]) | set(current["by_question_type"])):
        before = baseline["by_question_type"].get(kind)
        after = current["by_question_type"].get(kind)
        if before is None or after is None:
            lines.append(f"type {kind}: present in only one report")
            continue
        lines.append(f"-- {kind} ({after['questions']} questions)")
        for metric in TYPE_METRICS:
            row(f"  {metric}", before.get(metric), after.get(metric), type_tolerance)
            if lines[-1].endswith("REGRESSION"):
                regressions[-1] = f"{kind}: {regressions[-1]}"
    for metric in ("latency_ms_p50", "latency_ms_p95"):
        lines.append(
            f"{metric:<44} {baseline.get(metric)!s:>8} {current.get(metric)!s:>8}  (not judged)"
        )
    lost = [
        qid
        for qid, was in baseline.get("per_question", {}).items()
        if was["session_rank"] is not None
        and was["session_rank"] <= 5
        and (
            qid in current.get("per_question", {})
            and (
                current["per_question"][qid]["session_rank"] is None
                or current["per_question"][qid]["session_rank"] > 5
            )
        )
    ]
    gained = [
        qid
        for qid, now in current.get("per_question", {}).items()
        if now["session_rank"] is not None
        and now["session_rank"] <= 5
        and qid in baseline.get("per_question", {})
        and (
            baseline["per_question"][qid]["session_rank"] is None
            or baseline["per_question"][qid]["session_rank"] > 5
        )
    ]
    lines.append(f"questions that left the top 5 sessions: {len(lost)} {lost[:10]}")
    lines.append(f"questions that entered the top 5 sessions: {len(gained)} {gained[:10]}")
    return lines, regressions


def comparable(baseline: dict[str, Any], current: dict[str, Any]) -> list[str]:
    problems = []
    for key in (
        "pooled",
        "as_of_question_date",
        "top_k",
        "depth",
        "budget",
        "questions",
        "reranker",
    ):
        if baseline.get(key) != current.get(key):
            problems.append(f"{key}: {baseline.get(key)!r} vs {current.get(key)!r}")
    before = (baseline.get("data") or {}).get("sha256")
    after = (current.get("data") or {}).get("sha256")
    if before != after:
        problems.append("data: the reports were measured on different files")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, help="LongMemEval-format JSON")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--questions", type=int, help="Use only the first N questions")
    parser.add_argument("--per-type", type=int, help="Use only the first N questions of each type")
    parser.add_argument("--types", help="Comma-separated question types to keep")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument(
        "--budget",
        type=int,
        default=BUDGET,
        help="Briefing token budget; the default is the largest the contract allows, so the "
        "number of results asked for, not the budget, limits the ranked list",
    )
    parser.add_argument("--no-pool", dest="pool", action="store_false")
    parser.add_argument(
        "--no-as-of",
        dest="as_of",
        action="store_false",
        help="Do not brief as of the question date",
    )
    parser.add_argument("--gate", action="store_true", help="Fail when a threshold is missed")
    parser.add_argument(
        "--gate-file", type=Path, default=GATE, help="Thresholds for --gate (default: committed)"
    )
    parser.add_argument(
        "--compare", type=Path, help="Baseline report; exit 1 on a regression beyond tolerance"
    )
    parser.add_argument("--current", type=Path, help="With --compare: a saved report, no new run")
    parser.add_argument("--tolerance", type=float, default=0.02, help="Overall drop allowed")
    parser.add_argument(
        "--type-tolerance", type=float, default=0.10, help="Drop allowed in one question type"
    )
    parser.add_argument(
        "--reranker-model",
        type=Path,
        help="Installed local cross-encoder directory; every briefing then asks for `rerank`",
    )
    args = parser.parse_args()
    if args.current is None:
        if args.data is None or args.output is None:
            parser.error("--data and --output are required unless --current is given")
        reranker = None
        if args.reranker_model is not None:
            from memoryking.adapters.local_embeddings import LocalOnnxReranker

            reranker = LocalOnnxReranker(args.reranker_model)
        raw = args.data.read_bytes()
        questions = load(
            args.data,
            args.questions,
            set(args.types.split(",")) if args.types else None,
            args.per_type,
        )

        def progress(done: int, total: int) -> None:
            if total > 1 and (done % 10 == 0 or done == total):
                print(f"{done}/{total} questions", file=sys.stderr, flush=True)

        report = run(
            questions,
            top_k=args.top_k,
            pool=args.pool,
            as_of=args.as_of,
            budget=args.budget,
            reranker=reranker,
            progress=progress,
        )
        report["data"] = {"path": args.data.name, "sha256": hashlib.sha256(raw).hexdigest()}
        report["gate"] = json.loads(args.gate_file.read_text()) if args.gate_file.exists() else {}
        report["failures"] = gate(report, report["gate"]) if args.gate else []
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(
            json.dumps(
                {k: v for k, v in report.items() if k not in {"gate", "data", "per_question"}}
            )
        )
    else:
        report = json.loads(args.current.read_text())
        report["failures"] = []
    status = 1 if report["failures"] else 0
    if args.compare is not None:
        baseline = json.loads(args.compare.read_text())
        problems = comparable(baseline, report)
        if problems:
            print("reports are not comparable: " + "; ".join(problems), file=sys.stderr)
            return 2
        lines, regressions = compare(
            baseline, report, tolerance=args.tolerance, type_tolerance=args.type_tolerance
        )
        print("\n".join(lines))
        if regressions:
            print(
                f"\n{len(regressions)} regression(s) beyond tolerance "
                f"({args.tolerance} overall, {args.type_tolerance} per question type):"
            )
            print("\n".join("  " + r for r in regressions))
            status = 1
        else:
            print("\nno regression beyond tolerance")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
