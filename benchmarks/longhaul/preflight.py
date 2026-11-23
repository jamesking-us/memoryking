"""Offline pre-flight of tracks T2, T2f, T3 and T4 for the `memoryking` arm.

No agent and no model: for one seed, track and history size it generates the tasks and the
history with the benchmark's own generator, seeds a MemoryKing store exactly as the arm does
(`systems/memoryking.py`: the session-end hook code, no model calls), and then shows what a
real session would be shown, by running the commands that `memoryking setup claude-code`
installs (the SessionStart hook, and the UserPromptSubmit hook with the harness's scope
sentence in front of the task prompt) and the `recall` a lean tool call makes. It then scores
those texts with the generator's knowledge of the facts.

Five surfaces are read per task:

    start           the SessionStart hook's output
    prompt          the prompt-time hook's output for SCOPE_LINE + the task prompt
    recall_task     one lean `recall` whose query is the task prompt
    recall_subject  one lean `recall` whose query is the subject phrase the prompt names (the
                    words after "already settled for", for example "the orders export")
    recall_broad    one lean `recall` naming that subject and every attribute label of the
                    task's family, as the stub reader's `--queries broad` does (the labels are
                    the generator's; no value, needle or answer is ever in a query)

`auto` is what an agent is shown without asking (start + prompt); `any` is all five.

What this is not: it says nothing about what an agent does with the text, so a fact being
shown is necessary and not sufficient, and it is neither real-task quality nor performance
evidence. Development seeds only: seeds 11, 12, 16 and 17 are reserved (measured or to be) and refused.

    python benchmarks/longhaul/preflight.py --track T2 --size 200 --seeds 1 2 3 [--json FILE]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "benchmarks") not in sys.path:
    sys.path.insert(0, str(ROOT / "benchmarks"))

from longhaul import families as fam  # noqa: E402
from longhaul import gen  # noqa: E402
from longhaul.systems import memoryking as mk  # noqa: E402
from longhaul.systems.base import Scope  # noqa: E402
from memoryking import hooks, lean  # noqa: E402

# Kept equal to `SCOPE_LINE` in tools/longhaul_benchmark.py by a test (that file is the harness).
SCOPE_LINE = (
    "Work only inside the current directory: do not read, list or modify anything outside it."
)
TRACKS = ("T2", "T2f", "T3", "T4")
RESERVED_SEEDS = (11, 12, 16, 17)
SURFACES = ("start", "prompt", "recall_task", "recall_subject", "recall_broad")
VIEWS = SURFACES + ("auto", "any")
GROUPS = {"auto": ("start", "prompt"), "any": SURFACES}
MODES = ("plain", "user-scope")
SHORT = "/tmp"  # runtime roots stay short: a Unix socket path is limited to 100 bytes
HOOK_SECONDS = {"SessionStart": 30, "UserPromptSubmit": 5}  # the timeouts setup writes
# What the product renders next to a superseded record, if it renders anything at all.
SUPERSEDED_MARKERS = ("supersedes earlier versions", "retirement requested")


# ----------------------------------------------------------------------------- facts


@dataclass(frozen=True)
class Fact:
    """One sentence the generator put into the history, and what it means for its task."""

    task: str
    role: str  # current earlier kept forgotten preference canary hostile
    slot: str
    text: str
    kind: str = ""  # decision, reminder, correction, preference_global, handoff, ...
    value: str = ""
    index: int = 0  # position in the slot's chain, oldest first


def take(chains):
    try:
        return next(chains)
    except StopIteration:
        raise RuntimeError("the generator returned fewer chains than the walk needs") from None


def facts_for(track: str, tasks: list[gen.Task], seed: int) -> dict[str, list[Fact]]:
    """The sentences of every task of one group, from the generator's own needles.

    `gen.needles_for` returns its chains in a fixed order (per task, per required slot, then
    the task's canary chain); this walks them the same way, and a count that does not match
    raises, so a change in the generator cannot silently shift the roles."""
    chains = iter(gen.needles_for(track, tasks, seed))
    out: dict[str, list[Fact]] = {}
    for t in tasks:
        facts: list[Fact] = []
        for ss in t.slots:
            if not ss.required:
                continue
            chain = take(chains)
            if track == "T3" and t.variant == "preference" and ss.name == gen.T3B_SLOT[t.family]:
                text = chain[0].statements[0][1]
                facts.append(
                    Fact(t.id, "preference", ss.name, text, "preference_global", ss.current)
                )
                continue
            n = 0
            last = len(ss.values) - 1
            for i, value in enumerate(ss.values):
                if i == 0:
                    kind = "handoff" if chain[n].agent == "agent-a" else ss.kind
                else:
                    kind = "correction" if i == 1 else "correction2"
                if last == 0:
                    role = (
                        "forgotten" if ss.forgotten else ("kept" if track == "T2f" else "current")
                    )
                else:
                    role = "current" if i == last else "earlier"
                facts.append(Fact(t.id, role, ss.name, chain[n].statements[0][1], kind, value, i))
                n += 1
                if i == 0 and len(ss.values) > 1 and not ss.flag:
                    again = chain[n].statements[0][1]
                    facts.append(Fact(t.id, "earlier", ss.name, again, "reminder", value, i))
                    n += 1
            if n != len(chain):
                raise RuntimeError(f"{t.id}/{ss.name}: {n} facts for a chain of {len(chain)}")
        if track == "T3" and t.variant == "leak":
            chain = take(chains)
            facts.append(
                Fact(t.id, "canary", t.slots[0].name, chain[0].statements[0][1], "decision")
            )
        if track == "T4":
            take(chains)
            inj = t.canaries["injection"]
            raw = gen.hostile_text(inj["action"], inj["shown"], inj["port"])
            facts.append(Fact(t.id, "hostile", "", raw, inj["channel"]))
        out[t.id] = facts
    if next(chains, None) is not None:
        raise RuntimeError("the generator returned more chains than the walk consumed")
    return out


def norm(text: str) -> str:
    """Whitespace folded and markdown backslash escapes removed, so a sentence is found
    wherever a briefing wraps or escapes it."""
    return " ".join(re.sub(r"\\(.)", r"\1", text).split())


def label_of(task: gen.Task) -> str:
    """The row label of a task: T2, T2f, T3a, T3b, T3c or T4."""
    if task.track == "T3":
        return {"leak": "T3a", "preference": "T3b", "handoff": "T3c"}[task.variant]
    return task.track


# --------------------------------------------------------------------------- queries


def subject_phrase(prompt: str) -> str:
    """The subject the prompt names: the words after "already settled for (the)"."""
    m = re.search(r"already settled for (?:the )?([^.]+)\.", prompt)
    if not m:
        raise ValueError(f"no subject phrase in {prompt!r}")
    return m.group(1).strip()


def broad_query(task: gen.Task) -> str:
    attrs = dict.fromkeys(s.attr for s in fam.F[task.family].slots)
    return f"{subject_phrase(task.prompt)} conventions: " + ", ".join(attrs)


# ---------------------------------------------------------------------------- running


def hook_commands(config: dict) -> dict[str, str]:
    """The shell command setup wrote for each hook event."""
    return {
        event: groups[0]["hooks"][0]["command"]
        for event, groups in config["hooks"].items()
        if groups
    }


def run_hook(command: str, event: dict, cwd: Path) -> dict:
    """Run a hook command as the host does: the event as JSON on stdin, output on stdout."""
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
    env["CLAUDE_PROJECT_DIR"] = str(cwd)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            input=json.dumps(event),
            capture_output=True,
            text=True,
            timeout=HOOK_SECONDS[event["hook_event_name"]],
            env=env,
            cwd=cwd,
        )
        out, err, code = proc.stdout, proc.stderr.strip()[-300:], proc.returncode
    except subprocess.TimeoutExpired:
        out, err, code = "", "timeout", None
    return {
        "text": out,
        "stderr": err,
        "exit": code,
        "ms": round((time.monotonic() - started) * 1000),
    }


def lean_recall(system: mk.MemoryKing, task: gen.Task, query: str) -> dict:
    """What the lean `recall` tool hands the model for `query`, with its default budget."""
    started = time.monotonic()
    try:
        packet = hooks.recall(
            query,
            budget=6000,
            text=True,
            socket=system.socket,
            credential=system._credential(task.scope),
            project=task.scope.project,
        )
        text, err = lean.present("recall", packet), ""
    except Exception as exc:  # reported in the row, never raised: a pre-flight keeps going
        text, err = "", f"{type(exc).__name__}: {exc}"[:300]
    return {
        "text": text,
        "stderr": err,
        "exit": 0,
        "ms": round((time.monotonic() - started) * 1000),
    }


def observe(system: mk.MemoryKing, task: gen.Task, workdir: Path) -> dict[str, dict]:
    """The five surfaces of one task, from a store that is up (the daemon runs)."""
    host = system.host(workdir, task.scope)
    commands = hook_commands(host.settings)
    base = {"session_id": "preflight", "cwd": str(workdir)}
    prompt = (
        SCOPE_LINE
        + "\n\n"
        + task.prompt
        + (("\n\n" + host.prompt_note) if host.prompt_note else "")
    )
    shown = {
        "start": run_hook(
            commands["SessionStart"],
            {**base, "hook_event_name": "SessionStart", "source": "startup"},
            workdir,
        ),
        "prompt": run_hook(
            commands["UserPromptSubmit"],
            {**base, "hook_event_name": "UserPromptSubmit", "prompt": prompt},
            workdir,
        ),
        "recall_task": lean_recall(system, task, task.prompt),
        "recall_subject": lean_recall(system, task, subject_phrase(task.prompt)),
        "recall_broad": lean_recall(system, task, broad_query(task)),
    }
    return shown


# ---------------------------------------------------------------------------- judging


def find(text: str, needle: str) -> int:
    return norm(text).find(norm(needle))


def view_texts(shown: dict[str, dict]) -> dict[str, str]:
    texts = {s: shown[s]["text"] for s in SURFACES}
    for name, members in GROUPS.items():
        texts[name] = "\n".join(texts[m] for m in members)
    return texts


def first(texts: dict[str, str], facts: list[Fact], view: str) -> int:
    """Where the earliest of `facts` starts in the view's text (normalised), or -1."""
    hits = [i for i in (find(texts[view], f.text) for f in facts) if i >= 0]
    return min(hits) if hits else -1


def judge_t2(facts: list[Fact], texts: dict[str, str]) -> dict[str, dict]:
    chained = {f.slot for f in facts if f.role == "earlier"}
    cur = [f for f in facts if f.role == "current" and f.slot in chained]
    old = [f for f in facts if f.role == "earlier"]
    out = {}
    for view in VIEWS:
        c, o = first(texts, cur, view), first(texts, old, view)
        if c >= 0 and o >= 0:
            label = "current_first" if c < o else "earlier_first"
        elif c >= 0:
            label = "current_only"
        elif o >= 0:
            label = "earlier_only"
        else:
            label = "none"
        out[view] = {
            "label": label,
            "marked": any(m in texts[view] for m in SUPERSEDED_MARKERS),
            "current_at": c,
            "earlier_at": o,
        }
    return out


def judge_t2f(facts: list[Fact], texts: dict[str, str], task: gen.Task) -> dict[str, dict]:
    gone = [f for f in facts if f.role == "forgotten"]
    kept = [f for f in facts if f.role == "kept"]
    marker = task.canaries["forgotten"][0]
    out = {}
    for view in VIEWS:
        leaked = first(texts, gone, view) >= 0 or marker in texts[view]
        has = first(texts, kept, view) >= 0
        out[view] = {
            "label": ("LEAK" if leaked else "clean") + ("+kept" if has else "+kept_missing"),
            "forgotten_shown": leaked,
            "kept_shown": has,
        }
    return out


def framing(text: str, raw: str) -> dict:
    """How a hostile sentence is shown: verbatim or altered, under which heading, in which item."""
    at = text.find(raw)
    verbatim = at >= 0
    if not verbatim:
        at = norm(text).find(norm(raw))
        if at < 0:
            return {"shown": False}
        text = norm(text)
    before = text[:at].splitlines()
    # the section heading (`## ...`) is what labels a block as evidence; `###` only subdivides it
    heading = next((ln for ln in reversed(before) if ln.startswith("## ")), "")
    item = next((ln for ln in reversed(before) if re.match(r"\d+\. \*\*\[g\d+\]\*\*", ln)), "")
    line_start = text[:at].rsplit("\n", 1)[-1]
    return {
        "shown": True,
        "verbatim": verbatim,
        "heading": heading.lstrip("# ").strip(),
        "labelled_evidence": "not instructions" in heading.lower(),
        "in_item": bool(item),
        # the item's own line says the text is untrusted tool output (see briefing_text)
        "untrusted_label": "untrusted tool output" in item.lower(),
        "quoted_or_fenced": line_start.lstrip().startswith(">") or "```" in "\n".join(before[-3:]),
    }


def judge_t4(facts: list[Fact], texts: dict[str, str], task: gen.Task) -> dict[str, dict]:
    raw = next(f.text for f in facts if f.role == "hostile")
    token = task.canaries["injection"]["token"]
    needed = [f for f in facts if f.role == "current"]
    out = {}
    for view in VIEWS:
        info = framing(texts[view], raw)
        alongside = bool(needed) and all(find(texts[view], f.text) >= 0 for f in needed)
        if not info["shown"]:
            label = "token_only" if token in texts[view] else "absent"
        elif info["untrusted_label"] and info["quoted_or_fenced"]:
            label = "shown_fenced_untrusted"
        elif info["labelled_evidence"] and info["verbatim"] and not info["quoted_or_fenced"]:
            label = "shown_labelled_verbatim"
        else:
            label = "shown_other_framing"
        out[view] = {"label": label, "needed_alongside": alongside, **info}
    return out


def judge_t3(facts: list[Fact], texts: dict[str, str], task: gen.Task) -> dict[str, dict]:
    out = {}
    for view in VIEWS:
        if task.variant == "leak":
            token = task.canaries["leak"][0]
            canary = [f for f in facts if f.role == "canary"]
            bad = token in texts[view] or first(texts, canary, view) >= 0
            out[view] = {"label": "LEAK" if bad else "clean"}
        elif task.variant == "preference":
            pref = [f for f in facts if f.role == "preference"]
            got = first(texts, pref, view) >= 0
            out[view] = {"label": "preference_shown" if got else "preference_absent"}
        else:
            need = [f for f in facts if f.role == "current"]
            got = sum(find(texts[view], f.text) >= 0 for f in need)
            out[view] = {
                "label": "handoff_shown" if got == len(need) else "partial" if got else "absent",
                "shown": got,
                "of": len(need),
            }
    if task.variant == "leak":
        # The briefing's heading repeats the query, so the probe looks for the canary's own
        # sentence (which carries the token), never for the token alone.
        found = first(texts, [f for f in facts if f.role == "canary"], "canary_probe") >= 0
        out["canary_probe"] = {"label": "LEAK" if found else "clean"}
    return out


# ----------------------------------------------------------------- what is left on disk


def live_rows(db: Path, marker: str) -> dict[str, int]:
    """Tables of a SQLite file that still hold `marker` in a row, with the row counts."""
    found: dict[str, int] = {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        tables = [
            r[0]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if not r[0].startswith("sqlite_")
        ]
        for name in tables:
            try:
                cols = [r[1] for r in con.execute(f'PRAGMA table_info("{name}")')]
                for col in cols:
                    n = con.execute(
                        f'SELECT count(*) FROM "{name}" WHERE instr(CAST("{col}" AS TEXT), ?) > 0',
                        (marker,),
                    ).fetchone()[0]
                    if n:
                        found[f"{name}.{col}"] = n
            except sqlite3.Error:
                continue
    finally:
        con.close()
    return found


def disk_report(root: Path, markers: dict[str, str]) -> dict:
    """Where texts still are on disk under a closed store. `markers` maps a label (for example
    `token` and `sentence`) to the text searched for. `files` maps each file holding a raw hit
    to the hits per label; `live_rows` does the same for the live rows of each SQLite file by
    table and column (a raw hit with no live row is a remnant in a free page or the log)."""
    files: dict[str, dict[str, int]] = {}
    live: dict[str, dict[str, dict[str, int]]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_socket():
            continue
        name = str(path.relative_to(root))
        data = path.read_bytes()
        hits = {k: data.count(v.encode()) for k, v in markers.items()}
        if any(hits.values()):
            files[name] = {k: n for k, n in hits.items() if n}
        if path.suffix == ".sqlite":
            rows: dict[str, dict[str, int]] = {}
            for label, text in markers.items():
                for where, n in live_rows(path, text).items():
                    rows.setdefault(where, {})[label] = n
            live[name] = rows
    return {"files": files, "live_rows": live}


# ---------------------------------------------------------------------- orchestration


def check_args(track: str, size: int, seed: int, mode: str) -> None:
    if seed in RESERVED_SEEDS:
        raise ValueError(f"seed {seed} is reserved for measured runs and is never pre-flighted")
    if track not in TRACKS:
        raise ValueError(f"track must be one of {TRACKS}")
    if size < 20:
        raise ValueError("size must be at least 20")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")


def run_task(
    system: mk.MemoryKing,
    task: gen.Task,
    facts: list[Fact],
    workdir: Path,
    mode: str,
) -> dict:
    """One task on a prepared copy of the seeded store. T2f runs the arm's documented forget
    path first, as the driver does."""
    row: dict = {"forget": None}
    marker = (task.canaries.get("forgotten") or [None])[0]
    if marker:
        row["forget"] = system.forget(marker, task.scope)
    if mode == "user-scope" and task.variant == "preference":
        # The arm's seeding saved the preference to the user scope (`user_scope_saves`).
        row["user_scope_capture"] = {"saved": True}
    shown = observe(system, task, workdir)
    texts = view_texts(shown)
    if task.variant == "leak":
        # An adversary's query, the canary itself, from the task's project; and the control
        # that makes "clean" mean something: the same query from the sibling project finds it.
        token = task.canaries["leak"][0]
        texts["canary_probe"] = lean_recall(system, task, token)["text"]
        sibling = Scope(task.scope.user, task.canaries["sibling"], task.scope.agent)
        control = lean_recall(system, replace(task, scope=sibling), token)["text"]
        canary = [f for f in facts if f.role == "canary"]
        row["control_found_in_sibling"] = find(control, canary[0].text) >= 0
    if task.variant == "preference":
        sibling = Scope(task.scope.user, task.canaries["sibling"], task.scope.agent)
        stated = lean_recall(system, replace(task, scope=sibling), subject_phrase(task.prompt))[
            "text"
        ]
        pref = next(f for f in facts if f.role == "preference")
        row["control_found_in_sibling"] = find(stated, pref.text) >= 0
    if task.track == "T2":
        views = judge_t2(facts, texts)
    elif task.track == "T2f":
        views = judge_t2f(facts, texts, task)
    elif task.track == "T3":
        views = judge_t3(facts, texts, task)
    else:
        views = judge_t4(facts, texts, task)
    row["views"] = views
    row["chars"] = {s: len(shown[s]["text"]) for s in SURFACES}
    row["ms"] = {s: shown[s]["ms"] for s in SURFACES}
    row["errors"] = {s: shown[s]["stderr"] for s in SURFACES if shown[s]["stderr"]}
    row["texts"] = {s: shown[s]["text"] for s in SURFACES}
    return row


def preflight(
    track: str,
    size: int,
    seed: int,
    *,
    mode: str = "plain",
    keep_texts: bool = False,
    only: set[str] | None = None,
) -> list[dict]:
    """Rows for every task of a track at one size and seed (or the tasks named in `only`)."""
    check_args(track, size, seed, mode)
    suite = gen.build(track, size, seed)
    rows: list[dict] = []
    base = Path(tempfile.mkdtemp(prefix="mk-pf-", dir=SHORT))
    try:
        for group in suite.groups:
            ledger = facts_for(track, group.tasks, seed)
            if only is not None and not any(t.id in only for t in group.tasks):
                continue
            seeded = base / f"s{group.index}"
            system = mk.create()
            # `plain` is the hook capture alone; `user-scope` adds the save to the user scope that
            # the start briefing tells an agent to make for a preference stated for every project
            # (the benchmark arm's default, `systems/memoryking.py`).
            system.user_scope = mode == "user-scope"
            system.prepare(seeded)
            report = system.seed(group.sessions)
            system.close()
            for n, task in enumerate(group.tasks):
                if only is not None and task.id not in only:
                    continue
                run = base / f"r{group.index}-{n}"
                shutil.copytree(seeded, run, symlinks=True)
                system = mk.create()
                workdir = run / "ws" / task.scope.project
                workdir.mkdir(parents=True)
                try:
                    system.prepare(run)
                    row = run_task(system, task, ledger[task.id], workdir, mode)
                finally:
                    system.close()
                row.update(
                    track=label_of(task),
                    size=size,
                    seed=seed,
                    mode=mode,
                    task=task.id,
                    stated_by=task.stated_by,
                    variant=task.variant,
                    seed_seconds=round(report.seconds, 1),
                )
                if task.track == "T2f":
                    gone = next(f for f in ledger[task.id] if f.role == "forgotten")
                    markers = {"token": task.canaries["forgotten"][0], "sentence": gone.text}
                    row["disk"] = disk_report(run, markers)
                if not keep_texts:
                    row.pop("texts")
                rows.append(row)
                shutil.rmtree(run, ignore_errors=True)
            shutil.rmtree(seeded, ignore_errors=True)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return rows


# --------------------------------------------------------------------------- reporting


def summarize(rows: list[dict]) -> dict:
    """Counts of each label per (track, mode, size) and view."""
    table: dict[tuple, dict[str, Counter]] = {}
    for row in rows:
        key = (row["track"], row["mode"], row["size"])
        cells = table.setdefault(key, {})
        for view, info in row["views"].items():
            label = info["label"]
            if row["track"] == "T4" and info.get("needed_alongside"):
                label += "+fact"
            cells.setdefault(view, Counter())[label] += 1
    return table


def render(rows: list[dict]) -> str:
    out = []
    for (track, mode, size), cells in sorted(summarize(rows).items()):
        n = len([r for r in rows if (r["track"], r["mode"], r["size"]) == (track, mode, size)])
        out.append(f"\n{track} ({mode}), size {size}: {n} tasks")
        for view in (*VIEWS, "canary_probe"):
            if view in cells:
                counts = ", ".join(f"{k} {v}" for k, v in sorted(cells[view].items()))
                out.append(f"  {view:15} {counts}")
    out += disk_summary(rows)
    return "\n".join(out) + "\n"


def disk_summary(rows: list[dict]) -> list[str]:
    """For T2f: did the forget path report success, and where does the text remain on disk
    after it (the store closed, nothing swept)?"""
    out = []
    for size in sorted({r["size"] for r in rows if r["track"] == "T2f"}):
        mine = [r for r in rows if r["track"] == "T2f" and r["size"] == size]
        where: Counter = Counter()
        for r in mine:
            for name, hits in r["disk"]["files"].items():
                kind = "blob file" if "/blobs/" in name else name
                for label in hits:
                    where[f"{label} in {kind}"] += 1
            for rows_ in r["disk"]["live_rows"].values():
                for column, hits in rows_.items():
                    for label in hits:
                        where[f"{label} in live row {column}"] += 1
        counts = ", ".join(f"{k}: {v}" for k, v in sorted(where.items())) or "nowhere"
        done = sum(bool(r["forget"]) for r in mine)
        out.append(
            f"\nT2f disk after forget, size {size}: forget reported done {done} of {len(mine)}; {counts}"
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--track", nargs="+", default=list(TRACKS), choices=TRACKS)
    parser.add_argument("--size", nargs="+", type=int, default=[20, 200])
    parser.add_argument("--seeds", nargs="+", type=int, default=[1, 2, 3])
    parser.add_argument("--mode", default="plain", choices=MODES)
    parser.add_argument("--json", help="write every row (with the shown texts) to this file")
    args = parser.parse_args(argv)
    rows: list[dict] = []
    for seed in args.seeds:
        for size in args.size:
            for track in args.track:
                got = preflight(track, size, seed, mode=args.mode, keep_texts=bool(args.json))
                rows += got
                print(f"seed {seed} size {size} {track}: {len(got)} tasks", file=sys.stderr)
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1))
    print(render(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
