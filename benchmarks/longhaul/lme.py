"""Track T5: standard question answering on a stratified subset of LongMemEval `_s`.

Each question comes with its own haystack of about 50 sessions. For each question in the
subset, one system ingests the haystack into an empty instance, hands back `context_for(question)`
under one character budget, a reader command answers from that context and a judge command
grades the answer. The reader and judge are commands given on the command line, the same for
every system; nothing here calls a model unless the operator passes one. The data file is
untrusted: it is only read as JSON and never committed.

Scores: accuracy overall and by question type (judge verdicts), and evidence recall: whether the
text of an answer-bearing turn (`has_answer`) of the question's evidence sessions is present
in the context the system returned. Recall is measured on the text itself, so it does not need
the system to keep provenance; a system that rewrites or summarizes what it stores can score
low on recall and still answer correctly.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .systems.base import Scope, Session, Turn

PROMPTS = Path(__file__).resolve().parent / "prompts"
DEFAULT_DATA = Path("/tmp/mk-lme/data/longmemeval_s_cleaned.json")
SCOPE = Scope("lme-user", "lme", "lme-agent")
PREFIX = 80  # characters of a turn compared in the context (systems may cut long turns)


def load(path: Path) -> tuple[list[dict], str]:
    raw = Path(path).read_bytes()
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("expected a list of question instances")
    return data, hashlib.sha256(raw).hexdigest()


def select(data: list[dict], size: int = 60, seed: int = 1) -> list[dict]:
    """A subset stratified by question type: each type's share of `size` follows its share of
    the file (largest remainders, at least one per type), the questions of a type are drawn
    with a fixed seed, and the result is sorted by question id."""
    return _stratified(data, size, seed)


#: Draws that stage T6 leaves out: the development draws (seeds 1 to 5) and stage T5's (seed 20), 60 each.
T6_EARLIER = tuple((seed, 60) for seed in (1, 2, 3, 4, 5, 20))
#: The stage T6 draw: 120 questions, seed 21, from the questions in none of `T6_EARLIER`.
T6_SIZE, T6_SEED = 120, 21
#: How many of the file's 500 questions are in none of the earlier draws (declared in RUNNING.md; the
#: stage refuses to run if the file gives another number).
T6_POOL = 232


def earlier_ids(data: list[dict], earlier=T6_EARLIER) -> set[str]:
    """The question ids of the given earlier draws, each `(seed, size)` made by `select`."""
    out: set[str] = set()
    for seed, size in earlier:
        out.update(item["question_id"] for item in select(data, size, seed))
    return out


def select_excluding(
    data: list[dict], size: int, seed: int, excluded: set[str] | frozenset[str]
) -> list[dict]:
    """The same stratified draw as `select`, from only the questions whose id is not in
    `excluded`: the shares of the types follow the shares in what is left, the questions of a type
    are drawn with the fixed seed from what is left of it, and the result is sorted by id.
    It refuses (instead of drawing fewer) when a type has fewer questions left than its share."""
    pool = [item for item in data if item["question_id"] not in excluded]
    return _stratified(pool, size, seed, strict=True)


def t6_pool(data: list[dict]) -> list[dict]:
    """The questions that are in none of the earlier draws of stage T6 (232 of the 500)."""
    skip = earlier_ids(data)
    return [item for item in data if item["question_id"] not in skip]


def t6_draw(data: list[dict], size: int = T6_SIZE, seed: int = T6_SEED) -> list[dict]:
    """The stage T6 draw, reproducible from the data file, the seed and this rule alone."""
    skip = earlier_ids(data)
    chosen = select_excluding(data, size, seed, skip)
    left = len(data) - len(skip & {i["question_id"] for i in data})
    if left != T6_POOL:
        raise ValueError(f"the pool of unused questions is {left}, not the declared {T6_POOL}")
    return chosen


def _stratified(data: list[dict], size: int, seed: int, strict: bool = False) -> list[dict]:
    types: dict[str, list[dict]] = {}
    for item in data:
        types.setdefault(item["question_type"], []).append(item)
    names = sorted(types)
    if size < len(names):
        raise ValueError(f"size must be at least {len(names)} (one per question type)")
    total = len(data)
    exact = {t: size * len(types[t]) / total for t in names}
    take = {t: max(1, math.floor(exact[t])) for t in names}
    for t in sorted(names, key=lambda t: (exact[t] - math.floor(exact[t]), t), reverse=True):
        if sum(take.values()) >= size:
            break
        take[t] += 1
    while sum(take.values()) > size:  # the one-per-type floor can overshoot
        t = max(names, key=lambda t: (take[t] - exact[t], t))
        take[t] -= 1
    chosen = []
    for t in names:
        pool = sorted(types[t], key=lambda i: i["question_id"])
        if strict and take[t] > len(pool):
            raise ValueError(f"type {t!r} has {len(pool)} questions left, fewer than its {take[t]}")
        rng = random.Random(f"{seed}:{t}")
        chosen.extend(rng.sample(pool, min(take[t], len(pool))))
    return sorted(chosen, key=lambda i: i["question_id"])


def ids_digest(items: list[dict]) -> str:
    """SHA-256 of the sorted question ids of a draw, one per line: what a record states about a draw."""
    ids = sorted(i["question_id"] for i in items)
    return hashlib.sha256(("\n".join(ids) + "\n").encode()).hexdigest()


def stamp(date: str) -> str:
    """`2023/05/20 (Sat) 02:21` as RFC 3339."""
    for fmt in ("%Y/%m/%d (%a) %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%d"):
        try:
            return (
                datetime.strptime(date.strip(), fmt)
                .replace(tzinfo=timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ")
            )
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {date!r}")


def sessions_of(item: dict) -> list[Session]:
    out = []
    for sid, date, turns in zip(
        item["haystack_session_ids"], item["haystack_dates"], item["haystack_sessions"], strict=True
    ):
        out.append(
            Session(
                id=re.sub(r"[^A-Za-z0-9_-]", "-", sid),
                scope=SCOPE,
                started=stamp(date),
                turns=tuple(Turn(t["role"], t["content"]) for t in turns if t["content"].strip()),
            )
        )
    return out


def norm(text: str) -> str:
    return " ".join(text.split()).lower()


def evidence(item: dict, context: str) -> dict:
    """Which answer-bearing turns of the evidence sessions appear in the context."""
    seen = norm(context)
    gold = set(item["answer_session_ids"])
    turns = hits = 0
    sessions_hit = set()
    for sid, session in zip(item["haystack_session_ids"], item["haystack_sessions"], strict=True):
        if sid not in gold:
            continue
        for turn in session:
            if not turn.get("has_answer"):
                continue
            turns += 1
            if norm(turn["content"])[:PREFIX] in seen:
                hits += 1
                sessions_hit.add(sid)
    return {
        "answer_turns": turns,
        "answer_turns_found": hits,
        "evidence_sessions": len(gold),
        "evidence_sessions_found": len(sessions_hit),
        "any_found": hits > 0,
        "all_found": turns > 0 and hits == turns,
    }


def reader_prompt(item: dict, context: str) -> str:
    template = (PROMPTS / "t5_reader.md").read_text()
    return template.format(
        context=context or "(nothing was retrieved)",
        question=item["question"],
        question_date=item.get("question_date", ""),
    )


def judge_prompt(item: dict, answer: str) -> str:
    template = (PROMPTS / "t5_judge.md").read_text()
    notes = json.loads((PROMPTS / "t5_judge_notes.json").read_text())
    qtype = item["question_type"]
    note = (
        notes["abstention"]
        if item["question_id"].endswith("_abs")
        else notes.get(qtype, notes["default"])
    )
    return template.format(
        question=item["question"], reference=item["answer"], answer=answer, type_note=note
    )


def ask(command: str, prompt: str, timeout: int = 600, cwd=None, env=None) -> str:
    """Run a model command with the prompt on its standard input; its standard output is the
    reply. The command is split like a shell would and run without a shell."""
    proc = subprocess.run(
        shlex.split(command),
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


def parse_verdict(reply: str) -> bool | None:
    """True for `yes`, False for `no` as the first word of the reply; None when neither."""
    words = re.findall(r"[A-Za-z]+", reply.strip().lower())
    if words and words[0] in ("yes", "no"):
        return words[0] == "yes"
    return None


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def score(rows: list[dict]) -> dict:
    """Accuracy and evidence recall, overall and by question type. A reply the judge did not
    answer yes or no counts as wrong and is listed as `unparsed`."""

    def summary(part: list[dict]) -> dict:
        n = len(part)
        right = sum(1 for r in part if r["correct"])
        lo, hi = wilson(right, n)
        with_gold = [r for r in part if r["evidence"]["answer_turns"] > 0]
        return {
            "n": n,
            "correct": right,
            "accuracy": right / n if n else None,
            "ci95": [lo, hi],
            "unparsed": sum(1 for r in part if r["verdict"] is None),
            "recall_any": (
                sum(1 for r in with_gold if r["evidence"]["any_found"]) / len(with_gold)
                if with_gold
                else None
            ),
            "recall_all": (
                sum(1 for r in with_gold if r["evidence"]["all_found"]) / len(with_gold)
                if with_gold
                else None
            ),
            "context_chars_mean": (sum(r["context_chars"] for r in part) / n) if n else None,
        }

    types = sorted({r["type"] for r in rows})
    return {
        "overall": summary(rows),
        "by_type": {t: summary([r for r in rows if r["type"] == t]) for t in types},
    }
