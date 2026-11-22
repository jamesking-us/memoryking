"""Offline measure of what MemoryKing's hooks show an agent, on a seeded long history.

No agent and no model: for each task of a seeded group it runs the commands `memoryking setup
claude-code` installs, on the real task prompt (with the harness's scope sentence in front), and
asks whether each needle's own sentence is in what was shown:

  (a) the prompt-time recall hook's output (`UserPromptSubmit`),
  (b) the start briefing (`SessionStart`),
  (c) one `recall` call with the whole task prompt as the query (as the lean tool does, default
      budget, no limit),
  (d) one `recall` call with the query a careful agent would write (`careful_query`).

A needle is *carried* when its sentence is in the text (compared on letters and digits only, so
markdown escaping does not matter). A needle sentence is found from the generated history: in the
task's needle sessions, a sentence that names the task's subject and the slot's attribute and
states the slot's current value. It is never used to build a query.

This measures what reached the model's context, not whether an agent used it: a scripted reader,
not an agent. It says nothing about real-task quality.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from longhaul import families as fam
from longhaul import gen

SCOPE_LINE = (
    "Work only inside the current directory: do not read, list or modify anything outside it."
)
SOURCES = ("hook", "start", "recall_prompt", "recall_careful")
HOOK_SECONDS = 20  # the probe's own limit on a hook command (the host allows 5 seconds)


def flat(text: str) -> str:
    """Letters and digits of `text`, lower case, single spaces: what a sentence is matched on."""
    return " ".join(re.findall(r"[^\W_]+", text.casefold()))


def sentences(text: str) -> list[str]:
    out: list[str] = []
    for line in text.split("\n"):
        out.extend(s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z(])", line) if s.strip())
    return out


def needle_sentences(task: gen.Task, sessions: dict[str, gen.Session]) -> list[dict]:
    """For each required slot, the sentences of the needle sessions that state its current value."""
    family = fam.F[task.family]
    found = []
    for slot in task.slots:
        if not slot.required or slot.forgotten:
            continue
        spec = gen.slot_of(family, slot.name)
        wanted: list[str] = []
        for sid in task.needle_sessions:
            ses = sessions.get(sid)
            if ses is None:
                continue
            for turn in ses.turns:
                if turn.role == "tool":
                    continue
                for sentence in sentences(turn.text):
                    if spec.flag is not None:
                        state = spec.flag[0 if slot.current == "on" else 1]
                        if state[:60] in sentence:
                            wanted.append(sentence)
                        continue
                    if family.subject not in sentence or spec.attr not in sentence:
                        continue
                    if any(w in sentence for w in ("tried", "did not adopt", "TODO", "draft")):
                        continue
                    forms = {w for v, w in spec.pool if v == slot.current} | {slot.current}
                    if any(
                        re.search(rf"(?<![\w.]){re.escape(f)}(?![\w])", sentence) for f in forms
                    ):
                        wanted.append(sentence)
        found.append({"slot": slot.name, "sentences": sorted(set(wanted))})
    return found


def careful_query(prompt: str) -> str:
    """The query a careful agent writes from the prompt alone: the phrase the request names as
    the thing to follow ("settled for <phrase>"), plus the words of the function names the prompt
    gives in backticks (`write_orders` gives "write orders"; paths and calls with `(` or `/` are
    not names). Fixed rule, blind to the history."""
    phrase = re.search(r"settled for ([^.]+)\.", prompt)
    words = [phrase.group(1)] if phrase else []
    for name in re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)\(", prompt) or []:
        words.append(name.replace("_", " "))
    for name in re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", prompt):
        if "_" in name:
            words.append(name.replace("_", " "))
    return " ".join(words) or prompt


def carried(text: str, needles: list[dict]) -> list[bool]:
    body = flat(text)
    return [any(flat(s) in body for s in n["sentences"]) for n in needles]


def hook_commands(settings: dict) -> dict[str, str]:
    hooks = settings.get("hooks", {})
    return {
        "prompt": hooks["UserPromptSubmit"][0]["hooks"][0]["command"],
        "start": hooks["SessionStart"][0]["hooks"][0]["command"],
    }


def run_hook(command: str, event: dict, cwd: Path) -> tuple[str, float]:
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
    began = time.perf_counter()
    proc = subprocess.run(
        command,
        shell=True,
        input=json.dumps(event),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=HOOK_SECONDS,
    )
    return proc.stdout, time.perf_counter() - began


def probe_group(
    suite: gen.Suite,
    group: gen.Group,
    seeded: Path,
    work: Path,
    *,
    keep_text: bool = False,
    prompt_args: str = "",
) -> list[dict]:
    """One row per task of the group, measured on a copy of the seeded store."""
    from memoryking import hooks as mk_hooks

    from longhaul.systems import load as load_system

    root = work / f"{suite.seed}-{suite.size}-g{group.index}"
    if root.exists():
        shutil.rmtree(root)
    shutil.copytree(seeded, root, symlinks=True)
    system = load_system("memoryking")
    system.prepare(root)
    sessions = {s.id: s for s in group.sessions}
    rows = []
    try:
        for task in group.tasks:
            workdir = root / "ws" / task.scope.project
            workdir.mkdir(parents=True, exist_ok=True)
            host = system.host(workdir, task.scope)
            commands = hook_commands(host.settings)
            needles = needle_sentences(task, sessions)
            prompt = SCOPE_LINE + "\n\n" + task.prompt
            base = {"session_id": "probe", "cwd": str(workdir), "prompt_id": "p"}
            texts: dict[str, str] = {}
            seconds: dict[str, float] = {}
            texts["hook"], seconds["hook"] = run_hook(
                commands["prompt"] + (" " + prompt_args if prompt_args else ""),
                {**base, "hook_event_name": "UserPromptSubmit", "prompt": prompt},
                workdir,
            )
            texts["start"], seconds["start"] = run_hook(
                commands["start"],
                {**base, "hook_event_name": "SessionStart", "source": "startup"},
                workdir,
            )
            cred = system._credential(task.scope)
            for key, query in (
                ("recall_prompt", prompt),
                ("recall_careful", careful_query(task.prompt)),
            ):
                began = time.perf_counter()
                packet = mk_hooks.recall(
                    query[:2048],
                    budget=6000,
                    text=True,
                    socket=system.socket,
                    credential=cred,
                    project=task.scope.project,
                )
                seconds[key] = time.perf_counter() - began
                texts[key] = str(packet.get("text", ""))
            rows.append(
                {
                    "task": task.id,
                    "needles": len(needles),
                    "unmatched": [n["slot"] for n in needles if not n["sentences"]],
                    "carried": {k: carried(texts[k], needles) for k in SOURCES},
                    "chars": {k: len(texts[k]) for k in SOURCES},
                    "seconds": {k: round(seconds[k], 3) for k in SOURCES},
                    "query": careful_query(task.prompt),
                    **({"text": texts} if keep_text else {}),
                }
            )
    finally:
        system.close()
        shutil.rmtree(root, ignore_errors=True)
    return rows


def summarize(rows: list[dict]) -> dict:
    """Totals over tasks: needles carried (and tasks whose every needle is carried), mean
    characters, mean seconds, per source."""
    total = sum(r["needles"] for r in rows)
    out: dict = {"tasks": len(rows), "needles": total}
    for key in SOURCES:
        n = sum(sum(r["carried"][key]) for r in rows)
        whole = sum(1 for r in rows if all(r["carried"][key]))
        out[key] = {
            "needles": n,
            "tasks_complete": whole,
            "chars": round(sum(r["chars"][key] for r in rows) / max(1, len(rows))),
            "seconds": round(sum(r["seconds"][key] for r in rows) / max(1, len(rows)), 3),
            "max_seconds": max((r["seconds"][key] for r in rows), default=0),
        }
    return out


def render(summary: dict) -> str:
    lines = [
        f"{summary['tasks']} tasks, {summary['needles']} needles",
        "source           needles  tasks all  chars(mean)  seconds(mean/max)",
    ]
    for key in SOURCES:
        s = summary[key]
        lines.append(
            f"{key:<16} {s['needles']:>3}/{summary['needles']:<4} {s['tasks_complete']:>4}/"
            f"{summary['tasks']:<5} {s['chars']:>8}  {s['seconds']:>6.2f}/{s['max_seconds']:.2f}"
        )
    return "\n".join(lines)
