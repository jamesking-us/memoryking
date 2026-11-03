"""Judge-free evidence recall on LongMemEval-format data, through real briefings.

LongMemEval labels, for every question, which chat sessions hold the evidence and, per
turn, whether that turn carries the answer. That makes retrieval measurable without a
model judging anything: capture the sessions as observations, ask for a briefing with
the question, and count whether the cited observations come from the evidence sessions
(and turns). No answer is generated, so no LLM-as-judge is involved; this measures the
memory layer alone, which is also why the numbers are not comparable to the leaderboard's
answer-accuracy figures.

Data: pass `--data` with `longmemeval_oracle.json`, `longmemeval_s.json` or any file in
that format (download separately; nothing is committed). The oracle file holds only each
question's evidence sessions, so by default every question's sessions are pooled into one
project (`--pool`), which makes about 950 sessions compete per question. The `_s` file
already carries a per-question haystack; use `--no-pool` to brief each question against
its own sessions. `--as-of-question-date` (default on) briefs as of the question's date, so
sessions recorded later are excluded exactly as they would be for an agent at that time.

`--gate` compares the report with `fixtures/evaluation/evidence-recall-gate.json`; the
check runs it on the small committed sample so the suite stays offline.
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
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memoryking.adapters.local_store import LocalStore  # noqa: E402
from memoryking.adapters.retrieval import Retrieval  # noqa: E402

GATE = ROOT / "fixtures/evaluation/evidence-recall-gate.json"
TEXT_LIMIT = 60000


def session_ms(stamp: str) -> int:
    """`2023/05/20 (Sat) 02:21` or an ISO date-time, as Unix ms (UTC)."""
    text = stamp.strip()
    for fmt in ("%Y/%m/%d (%a) %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {stamp!r}")


def load(path: Path, limit: int | None, types: set[str] | None) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise ValueError("expected a list of question instances")
    items = [d for d in data if not types or d["question_type"] in types]
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


def run(
    questions: list[dict[str, Any]], *, top_k: int, pool: bool, as_of: bool, budget: int
) -> dict[str, Any]:
    def project_of(item: dict[str, Any]) -> str:
        return (
            "lme" if pool else "q-" + hashlib.sha256(item["question_id"].encode()).hexdigest()[:12]
        )

    # Every session once, oldest first, so the store records it at the session's date.
    sessions: dict[tuple[str, str], tuple[int, list[dict[str, Any]]]] = {}
    for item in questions:
        for sid, date, turns in zip(
            item["haystack_session_ids"],
            item["haystack_dates"],
            item["haystack_sessions"],
            strict=True,
        ):
            sessions.setdefault((project_of(item), sid), (session_ms(date), turns))
    ordered = sorted(sessions.items(), key=lambda entry: (entry[1][0], entry[0]))
    first_ms = ordered[0][1][0] if ordered else int(time.time() * 1000)
    clock = StampClock(first_ms - 1)
    with tempfile.TemporaryDirectory(prefix="mk-evidence-", dir="/tmp") as directory:
        root = Path(directory)
        credential = LocalStore.initialize(root / "db", root / "ctl").read_text()
        with LocalStore(root / "db", root / "ctl", clock=clock) as store:
            retrieval = Retrieval(store)
            captured: dict[str, tuple[str, bool]] = {}  # observation -> (session, has_answer)
            namespaces: dict[str, str] = {}
            started_capture = time.perf_counter()
            for (project, sid), (when, turns) in ordered:
                clock.advance_to(when)
                namespace = namespaces.get(project)
                if namespace is None:
                    namespace = namespaces[project] = store.namespace(credential, project)
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
                    captured[result["observation"]] = (sid, bool(turn.get("has_answer")))
                    # The daemon drains audit jobs between requests; the harness must too.
                    if len(captured) % 200 == 0:
                        while store.drain():
                            pass
            while store.drain():
                pass
            capture_seconds = time.perf_counter() - started_capture

            session_hits: list[float] = []
            turn_hits: list[float] = []
            reciprocal: list[float] = []
            tokens: list[int] = []
            latencies: list[float] = []
            by_type: dict[str, list[float]] = {}
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
                args: dict[str, Any] = {
                    "project": project_of(item),
                    "query": item["question"][:2048],
                    "bindings": [{}],
                    "max_output_tokens": budget,
                    "tokenizer": "utf8-byte-estimate-v1",
                    "top_k": top_k,
                }
                if as_of and when is not None:
                    args["as_of_ms"] = when
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
                cited = [g["reference"] for g in packet["guidance"]]
                found = [captured[c][0] for c in cited if c in captured]
                answer_turns = [captured[c][1] for c in cited if c in captured]
                session_hits.append(1.0 if evidence & set(found) else 0.0)
                turn_hits.append(1.0 if any(answer_turns) else 0.0)
                rank = next((n for n, s in enumerate(found, 1) if s in evidence), None)
                reciprocal.append(1 / rank if rank else 0.0)
                tokens.append(packet["usage"]["output_tokens"])
                by_type.setdefault(item["question_type"], []).append(session_hits[-1])
    return {
        "questions": len(questions),
        "observations": len(captured),
        "pooled": pool,
        "as_of_question_date": as_of,
        "top_k": top_k,
        "session_recall_at_k": round(statistics.fmean(session_hits), 4) if session_hits else None,
        "answer_turn_recall_at_k": round(statistics.fmean(turn_hits), 4) if turn_hits else None,
        "session_mrr": round(statistics.fmean(reciprocal), 4) if reciprocal else None,
        "by_question_type": {k: round(statistics.fmean(v), 4) for k, v in sorted(by_type.items())},
        "tokens_mean": round(statistics.fmean(tokens), 1) if tokens else None,
        "latency_ms_p50": round(statistics.median(latencies), 2) if latencies else None,
        "latency_ms_p95": round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 2)
        if latencies
        else None,
        "capture_seconds": round(capture_seconds, 2),
        "protocol": "judge_free_evidence_recall_v1: sessions are recorded at their own dates, each question is asked at its date, and the cited observations are scored against the labelled evidence sessions and answer turns; no answer is generated or judged",
    }


def gate(report: dict[str, Any], thresholds: dict[str, Any]) -> list[str]:
    failures = []
    for metric, bound in thresholds.get("minimum", {}).items():
        if report.get(metric) is None or report[metric] < bound:
            failures.append(f"{metric} {report.get(metric)} below {bound}")
    for metric, bound in thresholds.get("maximum", {}).items():
        if report.get(metric) is None or report[metric] > bound:
            failures.append(f"{metric} {report.get(metric)} above {bound}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True, help="LongMemEval-format JSON")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--questions", type=int, help="Use only the first N questions")
    parser.add_argument("--types", help="Comma-separated question types to keep")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--budget", type=int, default=6000)
    parser.add_argument("--no-pool", dest="pool", action="store_false")
    parser.add_argument(
        "--no-as-of",
        dest="as_of",
        action="store_false",
        help="Do not brief as of the question date",
    )
    parser.add_argument("--gate", action="store_true", help="Fail when a threshold is missed")
    args = parser.parse_args()
    raw = args.data.read_bytes()
    questions = load(args.data, args.questions, set(args.types.split(",")) if args.types else None)
    report = run(questions, top_k=args.top_k, pool=args.pool, as_of=args.as_of, budget=args.budget)
    report["data"] = {"path": str(args.data), "sha256": hashlib.sha256(raw).hexdigest()}
    report["gate"] = json.loads(GATE.read_text()) if GATE.exists() else {}
    report["failures"] = gate(report, report["gate"]) if args.gate else []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k not in {"gate", "data"}}))
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
