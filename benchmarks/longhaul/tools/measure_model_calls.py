#!/usr/bin/env python3
"""Measure how many model calls each product makes, against the local fake OpenAI-compatible
server (no real model, key or account).

For each installed product that needs a model: ingest SESSIONS synthetic sessions of TURNS
turns each (three user turns, six tool turns, three assistant turns for twelve), report calls and
estimated tokens per session, then answer RETRIEVALS retrieval queries and report calls and tokens
per retrieval. The table printed last extrapolates linearly to 20, 200 and 2,000 sessions: a
product whose per-session cost grows with the store (an update step that reads neighbours, say)
will cost more than that, and the measured per-session series is printed so the trend is visible.

Usage: measure_model_calls.py [--sessions 6] [--turns 12] [--retrievals 3] [--json out.json] [product ...]
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from benchmarks.longhaul.systems import _common as c  # noqa: E402
from benchmarks.longhaul.systems._types import Scope, Session, Turn  # noqa: E402

PRODUCTS = ["mcp_memory", "basic_memory", "mcp_memory_service", "claude_mem", "mem0", "graphiti"]
TOPICS = [
    ("invoice export", "zanzibar-7 queue"),
    ("customer table", "account_name column"),
    ("landing page", "teal palette"),
    ("retry policy", "three attempts with jittered backoff"),
    ("release train", "Thursday freeze at noon"),
    ("log format", "one JSON object per line"),
]


def make_session(i: int, turns: int, project: str = "billing") -> Session:
    topic, fact = TOPICS[i % len(TOPICS)]
    out: list[Turn] = []
    pattern = ["user", "tool", "tool", "assistant"]
    for k in range(turns):
        role = pattern[k % 4]
        if role == "user":
            out.append(
                Turn(
                    "user",
                    f"Session {i}: please work on the {topic}; remember we agreed on the {fact} (step {k}).",
                )
            )
        elif role == "tool":
            out.append(
                Turn(
                    "tool",
                    f"$ grep -rn '{topic.split()[0]}' src/\nsrc/{topic.split()[0]}.py:{10 + k}: value = '{fact}'",
                )
            )
        else:
            out.append(
                Turn(
                    "assistant", f"Done with step {k} on the {topic}; I kept the {fact} as agreed."
                )
            )
    return Session(
        id=f"m{i:04d}",
        scope=Scope(user="ada", project=project, agent="agent-1"),
        started=f"2026-02-{1 + i % 27:02d}T10:00:00Z",
        turns=tuple(out),
    )


def measure(module: str, n_sessions: int, turns: int, retrievals: int) -> dict:
    mod = importlib.import_module(f"benchmarks.longhaul.systems.{module}")
    if not mod.installed():
        return {"product": module, "skipped": "not installed"}
    system = mod.make()
    root = Path(tempfile.mkdtemp(prefix="mk-lhm-"))
    os.environ["LONGHAUL_FAKE_LLM"] = "1"
    os.environ.pop("LONGHAUL_LLM_BASE_URL", None)
    try:
        system.prepare(root)
        fake = getattr(system, "llm", None)
        series = []
        total_calls = total_tokens = 0
        for i in range(n_sessions):
            rep = system.seed(iter([make_session(i, turns)]))
            series.append(
                {"calls": rep.llm_calls, "tokens": rep.llm_tokens, "seconds": round(rep.seconds, 2)}
            )
            total_calls += rep.llm_calls
            total_tokens += rep.llm_tokens
        ret_calls = ret_tokens = 0
        for q in range(retrievals):
            before = fake.stats() if fake else None
            system.context_for(
                f"what did we agree about the {TOPICS[q % len(TOPICS)][0]}",
                Scope("ada", "billing", "agent-1"),
                4000,
            )
            if fake and before:
                after = fake.stats()
                ret_calls += after["chat_calls"] - before["chat_calls"]
                ret_tokens += (after["chat_prompt_tokens"] + after["chat_completion_tokens"]) - (
                    before["chat_prompt_tokens"] + before["chat_completion_tokens"]
                )
        emb = fake.stats() if fake else {}
        return {
            "product": module,
            "version": system.version,
            "sessions": n_sessions,
            "turns": turns,
            "ingest_calls_per_session": total_calls / n_sessions,
            "ingest_tokens_per_session": total_tokens / n_sessions,
            "per_session_series": series,
            "retrieval_calls": ret_calls / max(1, retrievals),
            "retrieval_tokens": ret_tokens / max(1, retrievals),
            "embedding_calls_to_fake": emb.get("embedding_calls", 0),
            "by_kind": emb.get("by_kind", {}),
        }
    finally:
        system.close()
        shutil.rmtree(root, ignore_errors=True)


def table(results: list[dict]) -> str:
    lines = [
        "| product | version | calls / session | tokens / session | calls / retrieval | 20 sessions | 200 sessions | 2,000 sessions |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        if "skipped" in r:
            lines.append(f"| {r['product']} | - | skipped: {r['skipped']} | | | | | |")
            continue
        cps, tps = r["ingest_calls_per_session"], r["ingest_tokens_per_session"]
        ext = " | ".join(
            f"{cps * n:,.0f} calls / {tps * n / 1000:,.0f}k tok" for n in (20, 200, 2000)
        )
        lines.append(
            f"| {r['product']} | {r['version']} | {cps:g} | {tps:,.0f} | {r['retrieval_calls']:g} | {ext} |"
        )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("products", nargs="*", default=PRODUCTS)
    ap.add_argument("--sessions", type=int, default=6)
    ap.add_argument("--turns", type=int, default=12)
    ap.add_argument("--retrievals", type=int, default=3)
    ap.add_argument("--json")
    a = ap.parse_args()
    results = [measure(p, a.sessions, a.turns, a.retrievals) for p in a.products]
    print(table(results))
    if a.json:
        Path(a.json).write_text(json.dumps(results, indent=1))
    _ = c  # the module import also validates the shared helpers


if __name__ == "__main__":
    main()
