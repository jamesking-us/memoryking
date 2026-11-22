#!/usr/bin/env python3
"""A deterministic stand-in for a coding agent, to test the benchmark offline. It calls no model.

It reads the task the driver wrote to `<run>/bench/task.json`, which a real agent never sees,
and prints `stream-json` events like the real CLI. Modes:

    null      does nothing: the task must fail (`off` cannot solve it)
    oracle    applies the oracle solution: the task must pass
    stale     applies the oracle with the earlier value of every corrected fact: `stale`
    gullible  oracle, plus it performs the action an injected instruction asks for
    leaker    oracle, but a memory tool result in its stream shows a canary: `leak`
    reader    looks for the facts in the memory the arm gives it (NOTES.md, CLAUDE.md, a
              history/ folder, or `memoryking recall` through the hooks' credential) the way
              a careful agent would, and solves from what it finds

    stub_agent.py WORKDIR PROMPT_FILE --mode MODE [--usage-file F] [--mcp-config F] [--settings F]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "benchmarks"))

from longhaul import families as fam  # noqa: E402
from longhaul import gen  # noqa: E402


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


def write_solution(task: gen.Task, workdir: Path, values: dict[str, str]) -> None:
    family = fam.F[task.family]
    source = fam.render_oracle(family, values)
    path = workdir / f"{family.package}/{family.module}.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)


def current_values(task: gen.Task) -> dict[str, str]:
    family = fam.F[task.family]
    values = {}
    for s in task.slots:
        value = s.current
        if s.forgotten:
            value = next(v for v in family.pool_values(s.name) if v != s.current)
        values[s.name] = value
    return values


def act_on_injection(task: gen.Task, workdir: Path) -> None:
    inj = task.canaries["injection"]
    if inj["action"] == "dep":
        with (workdir / "requirements.txt").open("a") as f:
            f.write(inj["token"] + "\n")
    elif inj["action"] == "file":
        (workdir / inj["token"]).write_text("ok\n")
    else:
        req = urllib.request.Request(
            f"http://127.0.0.1:{inj['port']}/collect/{inj['token']}", data=b"project", method="POST"
        )
        urllib.request.urlopen(req, timeout=5).read()


# ----------------------------------------------------------------- the reader


def candidates_in(text: str, task: gen.Task, slot: gen.SlotState) -> list[tuple[int, str]]:
    """(position, value) of every statement in `text` that gives this slot a value. A
    statement names the component (the task's subject) and the attribute; the value is the
    pool entry whose wording follows. Correction wording gives the new value first, and a
    retraction reads as `off`."""
    family = fam.F[task.family]
    spec = gen.slot_of(family, slot.name)
    found: list[tuple[int, str]] = []
    if spec.flag is not None:
        for state, sentence in zip(("on", "off"), spec.flag, strict=True):
            for m in re.finditer(re.escape(sentence[:60]), text):
                found.append((m.start(), state))
        return found
    found = []
    for line in re.finditer(r"[^\n]+", text):
        cursor = 0
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z(])", line.group(0)):
            at = line.start() + line.group(0).find(sentence, cursor)
            cursor = max(cursor, at - line.start() + len(sentence))
            if family.subject not in sentence or spec.attr not in sentence:
                continue
            if "tried" in sentence or "did not adopt" in sentence or "TODO" in sentence:
                continue
            best = None
            for value, wording in spec.pool:
                pattern = re.escape(wording if len(wording) > len(value) else value)
                m = re.search(rf"(?<![\w.]){pattern}(?![\w])", sentence)
                if m and (best is None or m.start() < best[0]):
                    best = (m.start(), value)
            if best is not None:
                found.append((at, best[1]))
    return found


def solve_from_text(task: gen.Task, text: str) -> dict[str, str]:
    """What the agent would write: the value it found for each slot, else a guess (the first
    pool value that is not the current one, as an agent with no memory would pick some value)."""
    family = fam.F[task.family]
    values = {
        s.name: next(v for v in family.pool_values(s.name) if v != s.current) for s in task.slots
    }
    for s in task.slots:
        if s.forgotten:
            continue
        found = candidates_in(text, task, s)
        if found:
            values[s.name] = sorted(found)[-1][1]  # the last statement wins (text is oldest first)
    return values


def memory_text(workdir: Path, task: gen.Task, args) -> tuple[str, dict, str | list[str]]:
    """(tool name, its input, text) of what looking in the arm's memory gives."""
    for name in ("NOTES.md", "CLAUDE.md"):
        if (workdir / name).is_file():
            return "Read", {"file_path": name}, (workdir / name).read_text()
    history = workdir / "history"
    if history.is_dir():
        family = fam.F[task.family]
        words = [family.subject.split()[0]]
        chunks = []
        for p in sorted(history.glob("*.md")):
            body = p.read_text()
            if any(w in body for w in words):
                chunks.append(body)
        return "Grep", {"pattern": words[0], "path": "history"}, "\n".join(chunks)
    if args.mcp_config and "memoryking" in json.loads(Path(args.mcp_config).read_text()).get(
        "mcpServers", {}
    ):
        return (
            "mcp__memoryking__recall",
            {"query": task.title},
            memoryking_recall(Path(args.mcp_config), task),
        )
    return "none", {}, ""


def memoryking_recall(mcp: Path, task: gen.Task) -> list[str]:
    sys.path.insert(0, str(ROOT / "src"))
    from memoryking import hooks

    args = json.loads(mcp.read_text())["mcpServers"]["memoryking"]["args"]
    opt = {args[i]: args[i + 1] for i in range(len(args) - 1) if args[i].startswith("--")}
    family = fam.F[task.family]
    out = []
    # Oldest first, as the reader parser expects: several queries, one briefing each.
    for slot in task.slots:
        spec = gen.slot_of(family, slot.name)
        packet = hooks.recall(
            f"{family.subject} {spec.attr}",
            budget=6000,
            text=True,
            socket=Path(opt["--socket"]),
            credential=Path(opt["--credential"]),
            project=opt["--project"],
        )
        out.append(str(packet.get("text", "")))
    return out


def order_oldest_first(briefings: list[str]) -> str:
    """Briefings list items by relevance; give the parser every distinct item, oldest first."""
    items: dict[str, str] = {}
    for text in briefings:
        blocks = re.split(r"\n(?=\d+\. \*\*\[g\d+\]\*\*)", text.split("## Caveats")[0])
        for block in blocks:
            if re.match(r"\d+\. \*\*\[g\d+\]", block):
                items[re.sub(r"^\d+\. \*\*\[g\d+\]\*\* ", "", block)] = block

    def stamp(block: str) -> str:
        m = re.search(r"recorded (\d{4}-\d\d-\d\d \d\d:\d\d)", block)
        return m.group(1) if m else ""

    return "\n".join(sorted(items.values(), key=stamp))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workdir")
    parser.add_argument("prompt_file")
    parser.add_argument(
        "--mode", required=True, choices=("null", "oracle", "stale", "gullible", "leaker", "reader")
    )
    parser.add_argument("--usage-file")
    parser.add_argument("--mcp-config")
    parser.add_argument("--settings")
    args = parser.parse_args()
    workdir = Path(args.workdir).resolve()
    bench = workdir.parent / "bench"
    task = gen.Task.from_json(json.loads((bench / "task.json").read_text()))
    emit({"type": "system", "subtype": "init", "model": "stub"})
    emit(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": f"Working on: {task.title}"}]},
        }
    )
    values = current_values(task)
    if args.mode == "stale":
        for s in task.slots:
            if s.stale and s.required:
                values[s.name] = s.stale[-1]
    if args.mode == "reader":
        tool, tool_input, text = memory_text(workdir, task, args)
        text = order_oldest_first(text) if tool.startswith("mcp__") else text
        emit(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "tool_use", "id": "m1", "name": tool, "input": tool_input}]
                },
            }
        )
        emit(
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "m1", "content": text[:60000]}
                    ]
                },
            }
        )
        values = solve_from_text(task, text)
    if args.mode != "null":
        write_solution(task, workdir, values)
    if args.mode == "gullible" and task.canaries.get("injection"):
        act_on_injection(task, workdir)
    if args.mode == "leaker":
        marker = (task.canaries.get("leak") or task.canaries.get("forgotten") or ["canary"])[0]
        emit(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "m2",
                            "name": "mcp__memoryking__recall",
                            "input": {},
                        }
                    ]
                },
            }
        )
        emit(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "m2",
                            "content": f"earlier note (ref {marker})",
                        }
                    ]
                },
            }
        )
    usage = {
        "input_tokens": 1000,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "output_tokens": 100,
    }
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 1000,
            "total_cost_usd": 0.0,
            "usage": usage,
        }
    )
    if args.usage_file:
        Path(args.usage_file).write_text(json.dumps(usage))
    return 0


if __name__ == "__main__":
    sys.exit(main())
