"""Stale-versus-current benchmark: after a fact is corrected, does a briefing return the new one?

Synthetic projects state facts ("The <entity> <attribute> is <value>"), correct them later,
and ask for the fact ("What is the <attribute> of <entity>?"). The correct evidence is the
newest statement. The three ways a correction reaches the store are all exercised through
the real daemon and client, the way an agent would use them:

* `capture`: a plain newer capture. Variants: marked (the correction says "now"), plain
  (the same sentence with the new value), terse (a short sentence that shares fewer words
  with the question), chatty (the new value buried in a long sentence) and repeated (the
  old fact was captured three times before it changed once).
* `memory_file`: an edit of a memory-tool file, as a new version of the same file
  (`str_replace`, or `create` over an existing file). The tool records the day, so both
  versions carry the same time and recency cannot be what separates them.
* `claim`: an accepted knowledge claim replaced by a later accepted revision of the same
  claim (two or three revisions). Only accepted claims in the briefing are scored here,
  because the claim is what is meant to carry the current answer.

Values are unique made-up words, so a guidance item states the old fact when its text
contains the old value and the new fact when it contains the new one. Per case:

* `current_returned`: the current statement is in the briefing's results (top eight);
* `current_first`: it is returned and ranked above every stale one (or no stale one is);
* `current_top1`: it is the first result that states either value;
* `stale_returned`: a stale statement is returned at all (lower is better);
* `stale_first`: a stale statement is ranked above the current one or the current is missing.

`--gate` compares the result with `fixtures/evaluation/stale-facts-gate.json`; a threshold
is set at what the code reliably achieves, so it is a regression gate and the failing cases
named in the report are the open work, not something to be argued away by loosening it.
Synthetic text only: this measures one behaviour, not real-task quality.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memoryking.adapters.local_ipc import Server  # noqa: E402
from memoryking.adapters.local_store import LocalStore  # noqa: E402
from memoryking.memory_tool import MemoryTool  # noqa: E402
from memoryking.sdk import LocalClient  # noqa: E402

GATE = ROOT / "fixtures/evaluation/stale-facts-gate.json"
SYLLABLES = "ka to ri mu ne so fa li do ve zu pa qi ro ba te".split()
ATTRIBUTES = "timeout region owner cadence limit retention window quota channel mode".split()
VARIANTS = {
    "capture": ["marked", "plain", "terse", "chatty", "repeated"],
    "memory_file": ["str_replace", "recreate"],
    "claim": ["two_revisions", "three_revisions"],
}
DAY_MS = 86_400_000
BASE_MS = 1_700_000_000_000
TOP_K = 8
FILLERS = 40


class Words:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.used: set[str] = set(ATTRIBUTES)

    def new(self) -> str:
        while True:
            word = "".join(self.rng.choice(SYLLABLES) for _ in range(3))
            if word not in self.used:
                self.used.add(word)
                return word


def span(observation: str, text: str) -> dict[str, Any]:
    return {
        "observation": observation,
        "start_byte": 0,
        "end_byte": len(text.encode()),
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
    }


def claim_body(evidence: dict[str, Any], text: str) -> dict[str, Any]:
    return {
        "text": text,
        "kind": "knowledge",
        "selector": {"selector_version": "1", "any_of": [{"all_of": [{"op": "any"}]}]},
        "valid_start": None,
        "valid_end": None,
        "task": None,
        "run": None,
        "readers": None,
        "evidence": [evidence],
        "authority": None,
        "rule_key": None,
        "effect": None,
        "rule_value": None,
        "provenance": {},
    }


class Project:
    """One project on the shared service: its client, tool and a counter for unique keys."""

    def __init__(self, server: Server, credential: Path, name: str):
        self.name = name
        self.client = LocalClient(server.path, credential, name)
        self.tool = MemoryTool(self.client)
        self.counter = 0

    def capture(self, text: str, at: int, source: str = "chat") -> str:
        self.counter += 1
        event = f"{self.name}-e{self.counter}"
        return self.client.capture(
            f"{self.name}-k{self.counter}",
            {
                "version": 1,
                "project": self.name,
                "source": source,
                "source_event": event,
                "text": text,
                "readers": None,
                "classification": "internal",
                "observed_at_ms": at,
            },
        )["observation"]

    def knowledge(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        self.counter += 1
        return self.client.knowledge(
            {
                "version": 1,
                "incarnation": self.client.binding[-1],
                "action": action,
                "key": f"{self.name}-c{self.counter}",
                "args": {"project": self.name, **args},
            }
        )

    def claim(self, key: str, version: int, observation: str, statement: str) -> tuple[str, int]:
        """Propose a revision of claim `key`, accept it, return (revision, family version)."""
        proposed = self.knowledge(
            "propose",
            {
                "logical_key": key,
                "expected_version": version,
                "body": claim_body(span(observation, statement), statement),
            },
        )
        accepted = self.knowledge(
            "review",
            {
                "revision": proposed["revision"],
                "expected_version": proposed["family_version"],
                "decision": "accept",
                "reason": "operator_review",
                "breadth_reviewed": True,
            },
        )
        return proposed["revision"], accepted["family_version"]

    def brief(self, query: str) -> list[dict[str, Any]]:
        packet = self.client.retrieval(
            {
                "version": 1,
                "incarnation": self.client.binding[-1],
                "action": "brief",
                "args": {
                    "project": self.name,
                    "query": query,
                    "bindings": [{}],
                    "max_output_tokens": 65536,
                    "tokenizer": "utf8-byte-estimate-v1",
                    "top_k": TOP_K,
                },
            }
        )
        return packet["guidance"]


def build_case(
    project: Project, words: Words, mechanism: str, variant: str, number: int, clock: list[int]
) -> dict[str, Any]:
    """State a fact, correct it, and return the question with the two values."""
    entity, old, new = words.new(), words.new(), words.new()
    attribute = words.rng.choice(ATTRIBUTES)
    clock[0] += DAY_MS
    then = clock[0]
    said_old = f"The {entity} {attribute} is {old}."
    if mechanism == "capture":
        for repeat in range(3 if variant == "repeated" else 1):
            project.capture(said_old if repeat == 0 else f"Reminder: {said_old}", then + repeat)
        later = then + 3_600_000
        project.capture(
            {
                "marked": f"Correction: the {entity} {attribute} is now {new}.",
                "plain": f"The {entity} {attribute} is {new}.",
                "terse": f"{entity} uses {new}.",
                "chatty": (
                    f"After the review on Friday the whole team agreed that from now on the "
                    f"{entity} {attribute} is {new} for every environment we run."
                ),
                "repeated": f"Correction: the {entity} {attribute} is now {new}.",
            }[variant],
            later,
        )
    elif mechanism == "memory_file":
        path = f"/memories/{project.name}/notes/n-{entity}.md"
        project.tool.execute({"command": "create", "path": path, "file_text": said_old + "\n"})
        if variant == "str_replace":
            project.tool.execute(
                {"command": "str_replace", "path": path, "old_str": old, "new_str": new}
            )
        else:
            project.tool.execute(
                {
                    "command": "create",
                    "path": path,
                    "file_text": f"The {entity} {attribute} is {new}.\n",
                }
            )
    else:
        statements = [old, new] + ([words.new()] if variant == "three_revisions" else [])
        new = statements[-1]
        version = 0
        for index, value in enumerate(statements):
            text = f"The {entity} {attribute} is {value}."
            evidence = project.capture(text, then + index * 3_600_000, source="evidence")
            _, version = project.claim(f"fact-{entity}", version, evidence, text)
    return {
        "id": f"{mechanism}/{variant}/{number}",
        "mechanism": mechanism,
        "variant": variant,
        "query": f"What is the {attribute} of {entity}?",
        "old": old,
        "new": new,
    }


def fill(project: Project, words: Words, clock: list[int]) -> None:
    """Other entities' facts that share the attribute words, so ranking has work to do."""
    for _ in range(FILLERS):
        clock[0] += 60_000
        project.capture(
            f"The {words.new()} {words.rng.choice(ATTRIBUTES)} is {words.new()}.", clock[0]
        )


def score(case: dict[str, Any], guidance: list[dict[str, Any]]) -> dict[str, Any]:
    items = guidance
    if case["mechanism"] == "claim":
        items = [g for g in guidance if g.get("authority") == "accepted_knowledge"]
    stale = [n for n, g in enumerate(items, 1) if case["old"] in g["text"]]
    current = [n for n, g in enumerate(items, 1) if case["new"] in g["text"]]
    first_current = current[0] if current else None
    first_stale = stale[0] if stale else None
    return {
        "current_returned": first_current is not None,
        "current_first": first_current is not None
        and (first_stale is None or first_current < first_stale),
        "current_top1": first_current is not None
        and (first_stale is None or first_current < first_stale)
        and first_current == min(current + stale),
        "stale_returned": first_stale is not None,
        "stale_first": first_stale is not None
        and (first_current is None or first_stale < first_current),
        "current_rank": first_current,
        "stale_rank": first_stale,
    }


METRICS = ("current_returned", "current_first", "current_top1", "stale_returned", "stale_first")


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"cases": len(results)}
    for metric in METRICS:
        out[f"{metric}_rate"] = round(
            statistics.fmean(1.0 if r[metric] else 0.0 for r in results), 4
        )
    return out


def run(per_variant: int, seed: int = 11) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="mk-stale-", dir="/tmp") as directory:
        root = Path(directory)
        credential = LocalStore.initialize(root / "db", root / "ctl")
        started = time.perf_counter()
        with LocalStore(root / "db", root / "ctl") as store, Server(store) as server:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            words = Words(seed)
            clock = [BASE_MS]
            for mechanism, variants in VARIANTS.items():
                project = Project(server, credential, mechanism.replace("_", "-"))
                cases = [
                    build_case(project, words, mechanism, variant, n, clock)
                    for variant in variants
                    for n in range(per_variant)
                ]
                fill(project, words, clock)
                for case in cases:
                    results.append({**case, **score(case, project.brief(case["query"]))})
        seconds = round(time.perf_counter() - started, 2)
    report: dict[str, Any] = {
        "protocol": "stale_facts_v1: facts are stated, corrected by a newer capture, a memory-file edit or a replacing accepted claim, and asked for through a real briefing (top 8); values are unique words, so a result states the old or the new fact when it contains that value",
        "per_variant": per_variant,
        "top_k": TOP_K,
        "fillers_per_project": FILLERS,
        "overall": summarize(results),
        "by_mechanism": {},
        "by_variant": {},
        "seconds": seconds,
    }
    for mechanism, variants in VARIANTS.items():
        rows = [r for r in results if r["mechanism"] == mechanism]
        report["by_mechanism"][mechanism] = summarize(rows)
        for variant in variants:
            report["by_variant"][f"{mechanism}/{variant}"] = summarize(
                [r for r in rows if r["variant"] == variant]
            )
    report["failing_cases"] = sorted(
        (
            {k: r[k] for k in ("id", "query", "current_rank", "stale_rank")}
            for r in results
            if not r["current_first"]
        ),
        key=lambda r: r["id"],
    )
    return report


def gate(report: dict[str, Any], thresholds: dict[str, Any]) -> list[str]:
    failures = []
    for scope, bounds in thresholds.get("minimum", {}).items():
        for metric, bound in bounds.items():
            value = _lookup(report, scope, metric)
            if value is None or value < bound:
                failures.append(f"{scope} {metric} {value} below {bound}")
    for scope, bounds in thresholds.get("maximum", {}).items():
        for metric, bound in bounds.items():
            value = _lookup(report, scope, metric)
            if value is None or value > bound:
                failures.append(f"{scope} {metric} {value} above {bound}")
    return failures


def _lookup(report: dict[str, Any], scope: str, metric: str) -> float | None:
    node: Any = report["overall"] if scope == "overall" else None
    if node is None:
        node = report["by_mechanism"].get(scope) or report["by_variant"].get(scope) or {}
    return node.get(metric)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--per-variant", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gate", action="store_true", help="Fail when a threshold is missed")
    args = parser.parse_args()
    report = run(args.per_variant)
    report["gate"] = json.loads(GATE.read_text()) if GATE.exists() else {}
    report["failures"] = gate(report, report["gate"]) if args.gate else []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k not in {"gate", "failing_cases"}}))
    for failure in report["failures"]:
        print("FAIL " + failure, file=sys.stderr)
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
