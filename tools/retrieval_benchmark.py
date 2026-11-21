"""Labelled retrieval benchmark on a deterministic synthetic corpus, with a release gate.

The corpus is generated from a seed: every query is built to have a known relevant set
(documents sharing most of its distinctive terms) and distractors (documents sharing one
term). Recall, precision, reciprocal rank, briefing tokens and latency are reported.

Four further labelled cases, each in its own project of the same store, measure what the
ranking rewrite targets: word forms (an inflected note must be found by the base words),
term rarity (the note with the one distinctive word must outrank notes with only common
words), crowding (the only note with every term among hundreds of newer single-term notes)
and tie order (a newer correction comes before the stale fact it replaces).
Three more measure passages and one answer per fact: a long document (the answer sits in
the middle of a 20-30 KB observation, which must be found and whose excerpt must contain
it, and `fetch` of the excerpt's byte range must return exactly the excerpt), duplicates
(five near-identical notes must come back as one item that names the other four) and
versions (three versions of one file must come back as the newest, naming the older two).
`--gate` compares the result with the thresholds in fixtures/evaluation/retrieval-gate.json
and exits non-zero when any threshold is missed, so ranking changes are measured rather
than assumed. Synthetic text only: this is a regression gate, not a quality claim.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memoryking.adapters.local_store import LocalStore  # noqa: E402
from memoryking.adapters.retrieval import Retrieval  # noqa: E402

GATE = ROOT / "fixtures/evaluation/retrieval-gate.json"
COMMON = "service request retry timeout cache deploy release config test build log".split()
SYLLABLES = "ka to ri mu ne so fa li do ve zu pa qi ro ba te".split()
FORM_PAIRS = [
    ("retry", "retries"),
    ("charge", "charging"),
    ("deploy", "deployed"),
    ("customer", "customers"),
    ("migrate", "migrating"),
    ("cache", "cached"),
    ("index", "indexes"),
    ("restart", "restarted"),
    ("fail", "failing"),
    ("timeout", "timeouts"),
    ("route", "routing"),
    ("token", "tokens"),
    ("limit", "limited"),
    ("alert", "alerts"),
    ("backup", "backups"),
    ("rollback", "rollbacks"),
    ("schedule", "scheduled"),
    ("upload", "uploading"),
    ("expire", "expired"),
    ("rotate", "rotated"),
]
WINDOW = 256  # the candidate budget the crowding case must overflow


def word(rng: random.Random) -> str:
    return "".join(rng.choice(SYLLABLES) for _ in range(3))


def corpus(
    seed: int, documents: int, queries: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Documents and labelled queries. Each query has three rare terms; relevant documents
    carry two or three of them, distractors exactly one, the rest none."""
    rng = random.Random(seed)
    rare = [word(rng) for _ in range(queries * 3)]
    docs: list[dict[str, Any]] = []
    labelled: list[dict[str, Any]] = []
    for q in range(queries):
        terms = rare[q * 3 : q * 3 + 3]
        for r in range(3):
            chosen = terms if r == 0 else rng.sample(terms, 2)
            filler = " ".join(rng.choice(COMMON) for _ in range(6))
            docs.append(
                {"text": f"{filler} {' '.join(chosen)} {filler}", "query": q, "role": "relevant"}
            )
        for _ in range(2):
            filler = " ".join(rng.choice(COMMON) for _ in range(8))
            docs.append(
                {"text": f"{filler} {rng.choice(terms)} {filler}", "query": q, "role": "distractor"}
            )
        labelled.append({"query": " ".join(terms), "relevant": [], "common": rng.choice(COMMON)})
    while len(docs) < documents:
        filler = " ".join(rng.choice(COMMON) for _ in range(10))
        docs.append({"text": filler + " " + word(rng), "query": None, "role": "noise"})
    rng.shuffle(docs)
    # Relevant indices were assigned before the shuffle; recompute them by role and query.
    for q, item in enumerate(labelled):
        item["relevant"] = [
            i for i, doc in enumerate(docs) if doc["query"] == q and doc["role"] == "relevant"
        ]
    return docs, labelled


def labelled_cases(
    store: Any, credential: str, namespace: str, brief: Any, fetch: Any, seed: int, crowded: int
) -> dict[str, float]:
    """Word forms, rarity, crowding, tie order, long documents, duplicates and versions."""
    rng = random.Random(seed + 1)
    clock = [10_000_000]

    def capture(project: str, key: str, text: str, observed: int | None = None) -> str:
        clock[0] += 1
        if clock[0] % 200 == 0:
            while store.drain():  # the daemon does this between requests; a bulk load must too
                pass
        return str(
            store.capture(
                credential,
                namespace,
                key,
                {
                    "version": 1,
                    "project": project,
                    "source": "benchmark",
                    "source_event": key,
                    "text": text,
                    "readers": None,
                    "classification": "internal",
                    "observed_at_ms": observed or clock[0],
                },
            )["observation"]
        )

    while store.drain():
        pass
    neutral = "alpha bravo delta echo gamma kilo lima mike oscar papa romeo sierra".split()

    def filler(count: int, avoid: tuple[str, ...] = ()) -> str:
        return " ".join(rng.choice([w for w in neutral if w not in avoid]) for _ in range(count))

    # Word forms: the note holds inflected forms, the query the base words.
    combinations = [(a, b) for a in range(len(FORM_PAIRS)) for b in range(a + 1, len(FORM_PAIRS))]
    rng.shuffle(combinations)
    forms: list[tuple[str, str]] = []
    for number, (a, b) in enumerate(combinations[:30]):
        (a_base, a_form), (b_base, b_form) = FORM_PAIRS[a], FORM_PAIRS[b]
        target = capture("forms", f"f{number}", f"{a_form} {b_form} {filler(6)}")
        capture("forms", f"fd{number}", f"{a_form} {filler(8)}")
        forms.append((f"{a_base} {b_base}", target))
    for number in range(60):
        capture("forms", f"fn{number}", filler(10))
    # Rarity: two common words plus one rare word; the rare-word note must come first.
    common = "service request cache deploy release config".split()
    for number in range(150):
        capture("rarity", f"rn{number}", " ".join(rng.sample(common, 4)) + " " + filler(4))
    rarity: list[tuple[str, str]] = []
    for number in range(20):
        c1, c2 = rng.sample(common, 2)
        rare = word(rng) + str(number)
        for copy in range(3):
            capture("rarity", f"rc{number}-{copy}", f"{c1} {c2} {filler(6)}")
        target = capture("rarity", f"rr{number}", f"{rare} {filler(6)}")
        rarity.append((f"{c1} {c2} {rare}", target))
    # Crowding: the one note with all three terms is oldest; every term has hundreds of newer
    # single-term notes, more than one candidate window holds.
    crowd: list[tuple[str, str]] = []
    for number in range(crowded):
        terms = [word(rng) + f"c{number}{i}" for i in range(3)]
        target = capture("crowded", f"ct{number}", f"Decision: {' '.join(terms)} stay together.")
        for term in terms:
            for copy in range(WINDOW + 2):
                capture("crowded", f"cx{number}{term}{copy}", f"Routine note {copy} about {term}.")
        crowd.append((" ".join(terms), target))
    # Ties: a stale fact and its newer correction say the same thing with different numbers.
    ties: list[tuple[str, str, str]] = []
    for number in range(20):
        thing = word(rng) + f"t{number}"
        stale = capture(
            "ties", f"ts{number}", f"The {thing} limit is thirty requests.", 1_000 + number
        )
        fresh = capture(
            "ties", f"tf{number}", f"The {thing} limit is sixty requests.", 2_000 + number
        )
        ties.append((f"{thing} limit", fresh, stale))

    # Long documents: the answer is one sentence in the middle of a 20-30 KB capture, among
    # short notes that each carry one query word.
    long_cases: list[tuple[str, str, str]] = []
    for number in range(8):
        marker = word(rng) + f"l{number}"
        other = word(rng) + f"m{number}"
        sentence = f"The finding: the {marker} queue stalled because {other} held the lock."
        size = rng.randint(20_000, 30_000)
        text = ""
        while len(text) < size // 2:
            text += f"The service handled request {rng.randint(1, 999)} and {filler(5)}. "
        text += sentence + " "
        while len(text) < size:
            text += f"The service handled request {rng.randint(1, 999)} and {filler(5)}. "
        target = capture("longdoc", f"ld{number}", text)
        for copy in range(3):
            capture(
                "longdoc", f"lx{number}-{copy}", f"Short note {copy} about {marker}. {filler(4)}"
            )
            capture(
                "longdoc", f"ly{number}-{copy}", f"Short note {copy} about {other}. {filler(4)}"
            )
        long_cases.append((f"{marker} {other} queue", target, sentence))
    # Duplicates: five near-identical notes and one different note on the same subject.
    duplicate_cases: list[tuple[str, list[str], str]] = []
    for number in range(8):
        thing = word(rng) + f"d{number}"
        notes = [
            capture(
                "dups",
                f"dn{number}-{run}",
                f"Flaky test test_{thing}_retry failed again on CI run {run}: AssertionError timeout.",
            )
            for run in range(5)
        ]
        different = capture(
            "dups", f"dd{number}", f"test_{thing}_retry gets a longer timeout budget."
        )
        duplicate_cases.append((f"test_{thing}_retry AssertionError timeout", notes, different))
    # Versions: a memory-tool file edited twice (`name`, `name__v2`, `name__v3`).
    version_cases: list[tuple[str, list[str]]] = []
    for number in range(8):
        thing = word(rng) + f"v{number}"
        versions = [
            capture("versions", f"plan{number}", f"Release plan for {thing}: ship in March."),
            capture("versions", f"plan{number}__v2", f"Release plan for {thing}: ship in April."),
            capture("versions", f"plan{number}__v3", f"Release plan for {thing}: ship in June."),
        ]
        version_cases.append((f"release plan {thing}", versions))

    def hit_rate(cases: list[tuple[str, str]], project: str, top_k: int = 8) -> float:
        found = [target in brief(query, project)[0][:top_k] for query, target in cases]
        return round(sum(found) / len(found), 4) if found else 1.0

    rare_first = [brief(query, "rarity")[0][:1] == [target] for query, target in rarity]
    newer_first = []
    for query, fresh, stale in ties:
        returned = brief(query, "ties")[0]
        newer_first.append(
            fresh in returned
            and (stale not in returned or returned.index(fresh) < returned.index(stale))
        )
    quoted: list[bool] = []
    for query, target, sentence in long_cases:
        item = next((g for g in brief(query, "longdoc")[3] if g["reference"] == target), None)
        exact = False
        if item is not None and sentence in item["text"]:
            exact = (
                fetch(target, item["excerpt_start_byte"], item["excerpt_end_byte"]) == item["text"]
            )
        quoted.append(exact)
    collapsed = []
    for query, notes, different in duplicate_cases:
        items = brief(query, "dups")[3]
        kept = [g for g in items if g["reference"] in notes]
        collapsed.append(
            len(kept) == 1
            and kept[0]["reference"] == notes[-1]
            and kept[0].get("also_recorded_in", {}).get("count") == 4
            and different not in kept[0]["also_recorded_in"]["references"]
        )
    newest = []
    for query, versions in version_cases:
        kept = [g for g in brief(query, "versions")[3] if g["reference"] in versions]
        newest.append(
            len(kept) == 1
            and kept[0]["reference"] == versions[-1]
            and set(kept[0].get("supersedes", {}).get("references", [])) == set(versions[:-1])
        )
    return {
        "long_document_quote_rate": round(sum(quoted) / len(quoted), 4),
        "duplicates_collapsed_rate": round(sum(collapsed) / len(collapsed), 4),
        "newest_version_rate": round(sum(newest) / len(newest), 4),
        "word_form_recall": hit_rate(forms, "forms", 3),
        "rare_first_rate": round(sum(rare_first) / len(rare_first), 4),
        "crowded_recall": hit_rate(crowd, "crowded", 3),
        "newer_first_rate": round(sum(newer_first) / len(newer_first), 4),
    }


def run(seed: int, documents: int, queries: int, top_k: int, crowded: int = 1) -> dict[str, Any]:
    docs, labelled = corpus(seed, documents, queries)
    recalls: list[float] = []
    precisions: list[float] = []
    reciprocal: list[float] = []
    window_recalls: list[float] = []
    tokens: list[int] = []
    latencies: list[float] = []
    with tempfile.TemporaryDirectory(prefix="mk-benchmark-", dir="/tmp") as directory:
        root = Path(directory)
        credential = LocalStore.initialize(root / "db", root / "ctl").read_text()
        with LocalStore(root / "db", root / "ctl") as store:
            namespace = store.namespace(credential, "bench")
            ids: list[str] = []
            for position, doc in enumerate(docs):
                result = store.capture(
                    credential,
                    namespace,
                    f"d{position}",
                    {
                        "version": 1,
                        "project": "bench",
                        "source": "benchmark",
                        "source_event": f"d{position}",
                        "text": doc["text"],
                        "readers": None,
                        "classification": "internal",
                        "observed_at_ms": position + 1,
                    },
                )
                ids.append(result["observation"])
            retrieval = Retrieval(store)

            def brief(
                query: str, project: str = "bench"
            ) -> tuple[list[str], int, float, list[dict[str, Any]]]:
                command = {
                    "version": 1,
                    "incarnation": store.incarnation,
                    "action": "brief",
                    "args": {
                        "project": project,
                        "query": query,
                        "bindings": [{}],
                        "max_output_tokens": 6000,
                        "tokenizer": "utf8-byte-estimate-v1",
                        "top_k": top_k,
                    },
                }
                started = time.perf_counter()
                emitted: list[bytes] = []
                retrieval.execute(credential, command).emit(emitted.append)
                elapsed = (time.perf_counter() - started) * 1000
                packet = json.loads(emitted[0])
                return (
                    [g["reference"] for g in packet["guidance"]],
                    packet["usage"]["output_tokens"],
                    elapsed,
                    packet["guidance"],
                )

            def fetch(reference: str, start: int, end: int) -> str:
                command = {
                    "version": 1,
                    "incarnation": store.incarnation,
                    "action": "fetch",
                    "args": {
                        "project": "longdoc",
                        "reference": reference,
                        "kind": "observation",
                        "start_byte": start,
                        "end_byte": end,
                        "max_output_tokens": 6000,
                        "tokenizer": "utf8-byte-estimate-v1",
                    },
                }
                emitted: list[bytes] = []
                retrieval.execute(credential, command).emit(emitted.append)
                return str(json.loads(emitted[0])["excerpt"])

            for item in labelled:
                relevant = {ids[i] for i in item["relevant"]}
                returned, used, elapsed, _ = brief(item["query"])
                hits = [r for r in returned if r in relevant]
                recalls.append(len(set(hits)) / len(relevant))
                precisions.append(len(hits) / len(returned) if returned else 0.0)
                rank = next((n for n, r in enumerate(returned, 1) if r in relevant), None)
                reciprocal.append(1 / rank if rank else 0.0)
                tokens.append(used)
                latencies.append(elapsed)
                # A common word widens the candidate set past the window; recent relevant
                # documents must still be found.
                returned_common, _, _, _ = brief(item["query"] + " " + item["common"])
                window_recalls.append(len(relevant & set(returned_common)) / len(relevant))
            extra = labelled_cases(store, credential, namespace, brief, fetch, seed, crowded)
    return {
        "seed": seed,
        "documents": len(docs),
        "queries": len(labelled),
        "top_k": top_k,
        "recall_mean": round(statistics.fmean(recalls), 4),
        "precision_mean": round(statistics.fmean(precisions), 4),
        "mrr": round(statistics.fmean(reciprocal), 4),
        "window_recall_mean": round(statistics.fmean(window_recalls), 4),
        "tokens_mean": round(statistics.fmean(tokens), 1),
        **extra,
        "latency_ms_p50": round(statistics.median(latencies), 2),
        "latency_ms_p95": round(sorted(latencies)[int(len(latencies) * 0.95) - 1], 2),
    }


def gate(report: dict[str, Any], thresholds: dict[str, Any]) -> list[str]:
    failures = []
    for metric, bound in thresholds.get("minimum", {}).items():
        if report[metric] < bound:
            failures.append(f"{metric} {report[metric]} below {bound}")
    for metric, bound in thresholds.get("maximum", {}).items():
        if report[metric] > bound:
            failures.append(f"{metric} {report[metric]} above {bound}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--documents", type=int, default=400)
    parser.add_argument("--queries", type=int, default=200)
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument(
        "--crowded", type=int, default=2, help="Crowding cases (each captures about 800 notes)"
    )
    parser.add_argument("--gate", action="store_true", help="Fail when a threshold is missed")
    args = parser.parse_args()
    report = run(args.seed, args.documents, args.queries, args.top_k, args.crowded)
    report["gate"] = json.loads(GATE.read_text()) if GATE.exists() else {}
    report["failures"] = gate(report, report["gate"]) if args.gate else []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k not in {"gate"}}))
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
