#!/usr/bin/env python3
"""Restartable driver of stage T6 of the long-history benchmark (LongMemEval question answering, four
arms, 120 questions of a draw that leaves out every earlier draw). Run it with the Python of the checkout
that holds the commit under test. The protocol is "Stage T6" in benchmarks/longhaul/RUNNING.md.

  longhaul_t6.py plan                 what this configuration would do (starts nothing, calls no model)
  longhaul_t6.py check [--draw]       offline checks: the arms' environments and versions, the go-ahead; with
                                      --draw (needs the go-ahead file) also the size of the pool of unused
                                      questions and the draw's digest; prints no question and no id
  longhaul_t6.py run                  start, or resume, every arm that has not finished (four at a time)
  longhaul_t6.py status               counts from the durable ledger and the faults listed

Options: --stub   offline: development draw (--draw-seed 1 to 5), the fake reader and judge, arms from --arms,
                  a state directory of its own; no model call, no go-ahead file
         --root DIR  state directory (default /tmp/mk-lh-t6)   --repo DIR  the checkout to run from

State: <root>/real-t6/ledger.jsonl is append-only and is written by this driver and by the harness it starts
(every reader and judge call, announced before it is made). A call announced and never answered is lost and
counts against the cap; it is NEVER made again. The harness refuses any call once the ledger holds 960
(the cap), counting calls of earlier, interrupted invocations; the count and the announcement are one step
under a lock, so arms running together cannot pass it. Starting again never repeats a finished question or a
call. Real mode refuses to start unless `<root>/GO-T6` holds the full hash of the commit the checkout is at,
with a clean tree.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "benchmarks"))

from longhaul import lme  # noqa: E402

# Longest ingest first (Basic Memory: minutes a haystack), the two MemoryKing arms (index wait), then the quick one.
ARMS = ["basic_memory", "memoryking_020", "memoryking_semantic", "mcp_memory_service"]
VERSIONS = {
    "basic_memory": "1.0.0",
    "memoryking_020": "0.2.0",
    "mcp_memory_service": "13.0.0",
}
SIZE, DRAW_SEED, BUDGET = 120, 21, 12000
CEILING = len(ARMS) * SIZE * 2  # 960 calls: a reader and a judge call for each question of each arm
PARALLEL = 4
GO_NAME = "GO-T6"
READER = (
    "claude -p --model sonnet --no-session-persistence --setting-sources '' "
    "--disable-slash-commands --tools ''"
)
lock = threading.RLock()
stop = threading.Event()


class Paths:
    def __init__(self, a):
        self.repo = Path(a.repo).resolve()
        self.py = os.environ.get("MKLH_PY") or sys.executable
        self.root = Path(a.root)
        self.base = self.root / (f"stub-s{a.draw_seed}" if a.stub else "real-t6")
        self.results, self.work, self.streams = (
            self.base / "results",
            self.base / "work",
            self.base / "streams",
        )
        self.ledger = self.base / "ledger.jsonl"
        self.go = self.root / GO_NAME


def note(P: Paths, **kw) -> None:
    kw["t"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with lock:
        P.ledger.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(P.ledger, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, (json.dumps(kw) + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)


def ledger(P: Paths) -> list[dict]:
    out = []
    if P.ledger.exists():
        for line in P.ledger.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def calls_spent(P: Paths) -> int:
    return sum(1 for e in ledger(P) if e.get("ev") in ("reader_start", "judge_start"))


def gate(a, P: Paths) -> None:
    """Refuses a real run without the go-ahead for exactly this clean commit, and any stub run
    that is not on a development draw."""
    if a.stub:
        if a.draw_seed not in (1, 2, 3, 4, 5):
            sys.exit("a stub run uses a development draw (1 to 5)")
        return
    if (a.size, a.draw_seed, a.budget) != (SIZE, DRAW_SEED, BUDGET):
        sys.exit(f"stage T6 is draw {DRAW_SEED}, {SIZE} questions, a budget of {BUDGET} characters")
    if not P.go.exists():
        sys.exit(
            f"no {P.go}: the go-ahead (a file holding the full hash of the commit to run) is not given"
        )
    want = P.go.read_text().strip()
    head = subprocess.run(
        ["git", "-C", str(P.repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(P.repo), "status", "--porcelain"], capture_output=True, text=True
    ).stdout.strip()
    if head != want:
        sys.exit(f"the checkout is at {head}, the go-ahead names {want}")
    if dirty:
        sys.exit("the checkout has uncommitted changes")


def arms_of(a) -> list[str]:
    return a.arms.split(",") if a.arms else list(ARMS)


def commands(a, P: Paths) -> tuple[str, str]:
    if a.stub:
        fake = f"{P.py} {P.repo}/benchmarks/longhaul/fake_model.py"
        return f"{fake} reader", f"{fake} judge"
    return READER, READER


def env_for() -> dict[str, str]:
    env = dict(os.environ)
    for k in ("LONGHAUL_ALLOW_HOST_AGENT", "LONGHAUL_CLAUDE_MEM_CALL_CAP"):
        env.pop(k, None)
    env["HF_HUB_DISABLE_XET"] = "1"
    return env


def kill_recorded(P: Paths, root: Path) -> None:
    """Stop processes a dead run left behind, only by PIDs recorded under its own root."""
    for rel in sorted(root.rglob("*.pid")) + sorted(root.rglob("pids.json")):
        try:
            raw = rel.read_text()
            try:
                data = json.loads(raw)
                pids = [int(v) for v in (data.values() if isinstance(data, dict) else data)]
            except ValueError:
                pids = [int(raw.strip())]
        except (OSError, ValueError, TypeError):
            continue
        for pid in pids:
            try:
                cmd = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
            except OSError:
                continue
            if str(P.base) in cmd or "/tmp/mk-lh" in cmd:
                try:
                    os.kill(pid, signal.SIGTERM)  # the recorded process only, never its group
                except OSError:
                    pass


def result_file(P: Paths, a, arm: str) -> Path:
    return P.results / "t5" / f"{arm}-{a.size}.json"


def run_arm(a, P: Paths, arm: str) -> None:
    if stop.is_set() or result_file(P, a, arm).exists():
        return
    reader, judge = commands(a, P)
    note(P, ev="arm_start", arm=arm)
    started = time.time()
    root = P.work / f"t5-{arm}"
    argv = [
        P.py, str(P.repo / "tools/longhaul_benchmark.py"), "t5-run",
        "--arm", arm, "--size", str(a.size), "--seed", str(a.draw_seed),
        "--budget-chars", str(a.budget), "--reader-command", reader, "--judge-command", judge,
        "--ledger", str(P.ledger), "--call-cap", str(a.cap),
        "--results", str(P.results), "--work", str(P.work),
    ]  # fmt: skip
    if not a.stub:
        argv.append("--t6")
    if a.data:
        argv += ["--data", a.data]
    cp = subprocess.run(argv, env=env_for(), capture_output=True, text=True, cwd=P.repo)
    sd = P.streams / f"t5-{arm}"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "driver.out").write_text(cp.stdout + "\n" + cp.stderr)
    kill_recorded(P, root)
    if cp.returncode != 0 or not result_file(P, a, arm).exists():
        note(P, ev="arm_fault", arm=arm, rc=cp.returncode, err=(cp.stderr or "")[-400:])
        return
    note(P, ev="arm_finish", arm=arm, secs=round(time.time() - started))


def cmd_run(a, P: Paths) -> None:
    gate(a, P)
    if calls_spent(P) >= a.cap:
        sys.exit("the call cap is spent")
    arms = arms_of(a)
    todo = [x for x in arms if not result_file(P, a, x).exists()]
    if a.limit:
        todo = todo[: a.limit]
    for d in (P.results, P.work, P.streams):
        d.mkdir(parents=True, exist_ok=True)
    note(P, ev="run_invocation", todo=todo, spent=calls_spent(P))
    print(
        f"T6 ({'stub' if a.stub else 'real'}, draw {a.draw_seed}): {len(arms)} arms, {len(todo)} to go, "
        f"{calls_spent(P)} of {a.cap} calls already announced",
        flush=True,
    )
    signal.signal(signal.SIGTERM, lambda *x: stop.set())
    with ThreadPoolExecutor(PARALLEL) as ex:
        list(ex.map(lambda arm: run_arm(a, P, arm), todo))
    done = all(result_file(P, a, x).exists() for x in arms)
    print("stage done" if done else "stopped with arms unfinished", flush=True)


def cmd_status(a, P: Paths) -> None:
    ev = ledger(P)
    reader = sum(1 for e in ev if e.get("ev") == "reader_start")
    judge = sum(1 for e in ev if e.get("ev") == "judge_start")
    arms = arms_of(a)
    print(
        f"reader calls {reader}, judge calls {judge} of {a.cap}; "
        f"arms finished {sum(1 for e in ev if e.get('ev') == 'arm_finish')} of {len(arms)}"
    )
    for arm in arms:
        qdir = P.results / "t5" / arm
        done = len([f for f in qdir.glob("*.json") if "." not in f.stem]) if qdir.exists() else 0
        print(f"  {arm}: {done} of {a.size} questions have a result file")
    for e in ev:
        if e.get("ev") == "arm_fault":
            print(f"  fault {e['t']} {e['arm']} rc={e['rc']}: {e['err'].strip()[-200:]}")


def cmd_plan(a, P: Paths) -> None:
    arms = arms_of(a)
    print(
        f"{len(arms)} arms x {a.size} questions x 2 calls = {len(arms) * a.size * 2} calls "
        f"(reader and judge); cap {a.cap}; draw seed {a.draw_seed}; budget {a.budget} chars; "
        f"{PARALLEL} arms at a time; order {arms}"
    )


def cmd_check(a, P: Paths) -> None:
    """Offline, no model: every arm's environment is there and reports its version."""
    from longhaul.systems import load as load_system

    out: dict = {"stage": "t6", "repo": str(P.repo), "arms": {}}
    problems = []
    for arm in arms_of(a):
        try:
            system = load_system(arm)
            version = system.version
            want = VERSIONS.get(arm)
            if want and version != want:
                problems.append(f"{arm} reports {version}, not {want}")
            out["arms"][arm] = version
        except Exception as exc:
            problems.append(f"{arm}: {exc}")
    if a.draw:
        gate(a, P)  # the go-ahead must exist before the draw is computed
        data, digest = lme.load(Path(a.data or lme.DEFAULT_DATA))
        chosen = lme.t6_draw(
            data, a.size, a.draw_seed
        )  # refuses a pool other than the declared 232
        out.update(
            data_sha256=digest,
            pool=lme.T6_POOL,
            size=len(chosen),
            draw_ids_sha256=lme.ids_digest(chosen),
        )
    out["problems"] = problems
    out["ok"] = not problems
    print(json.dumps(out, indent=1))
    if problems:
        sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--stub", action="store_true")
    p.add_argument("--draw-seed", type=int, default=DRAW_SEED)
    p.add_argument("--size", type=int, default=SIZE)
    p.add_argument("--budget", type=int, default=BUDGET)
    p.add_argument("--cap", type=int, default=CEILING, help="the call cap (960 for the stage)")
    p.add_argument("--arms", help="comma-separated arms (stub runs; the stage runs all four)")
    p.add_argument("--data")
    p.add_argument("--root", default="/tmp/mk-lh-t6")
    p.add_argument("--repo", default=str(REPO))
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--limit", type=int, help="start at most N arms")
    sub.add_parser("status")
    sub.add_parser("plan")
    c = sub.add_parser("check")
    c.add_argument("--draw", action="store_true")
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if not a.stub and (a.arms or a.cap != CEILING):
        sys.exit("the stage runs all four arms under the cap of 960")
    P = Paths(a)
    {"run": cmd_run, "status": cmd_status, "plan": cmd_plan, "check": cmd_check}[a.cmd](a, P)
    return 0


if __name__ == "__main__":
    sys.exit(main())
