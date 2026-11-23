#!/usr/bin/env python3
"""Long-history benchmark: does memory hold up when the history is long?

Prior sessions are seeded into each memory system through its own ingest path; then one agent
session per run does a task whose hidden check passes only if the right knowledge from the
history was used. Tracks: T1 needle at scale, T2 change over time (T2f forget), T3 scopes,
T4 hostile memory, T5 question answering on LongMemEval `_s`, T2b a small lived run. The tool
never calls a model: the agent (and for T5 the reader and judge) are commands you pass.
See benchmarks/longhaul/RUNNING.md.

    longhaul_benchmark.py generate --track T1 --size 200 --out DIR
    longhaul_benchmark.py seed     --track T1 --size 200 --group 0 --arm memoryking --seeded DIR
    longhaul_benchmark.py run      --track T1 --size 200 --task ID --arm ARM --agent CMD --seeded DIR
    longhaul_benchmark.py report   --results DIR [--json]
    longhaul_benchmark.py validate [--track T]            (stub agent: null fails, oracle passes)
    longhaul_benchmark.py hookprobe --track T1 --size 200 --seed 1 --seeded DIR   (offline, no agent)
    longhaul_benchmark.py plan     [--arms N]             (real-session counts of the full plan)
    longhaul_benchmark.py t5-select | t5-run | t5-report  (LongMemEval question answering)
    longhaul_benchmark.py lived    --task ID --arm ARM --agent CMD    (T2b)
"""

from __future__ import annotations

import argparse
import functools
import json
import math
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))

from longhaul import gen, grading, hookprobe, lme  # noqa: E402
from longhaul.systems import load as load_system  # noqa: E402

LONGHAUL = ROOT / "benchmarks" / "longhaul"
STUB = LONGHAUL / "stub_agent.py"
FAKE = LONGHAUL / "fake_model.py"
SHORT = Path("/tmp/mk-lh")  # runtime roots must be short: a Unix socket path is limited
ARMS = ("off", "notes", "log", "native", "memoryking")
SCOPE_LINE = (
    "Work only inside the current directory: do not read, list or modify anything outside it."
)
PASSTHROUGH = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "NODE_EXTRA_CA_CERTS",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "AWS_REGION",
    "AWS_PROFILE",
    "LONGHAUL_MODEL",
    "LONGHAUL_MAX_BUDGET",
)
# The pilot and every test generate with these seeds: their instances are development data and
# never a result. Measured stages use MEASURED_SEED (RUNNING.md, "Seeds"), and `report` leaves
# development-seed runs out unless asked.
DEVELOPMENT_SEEDS = (1, 2, 3, 4, 5)
MEASURED_SEED = 11
DEFAULT_RESULTS = Path("/tmp/mk-lh/results")
DEFAULT_SEEDED = Path("/tmp/mk-lh/seeded")
DEFAULT_WORK = Path("/tmp/mk-lh/work")


class BenchmarkError(Exception):
    pass


# --------------------------------------------------------------------- suites


@functools.lru_cache(maxsize=32)
def suite(track: str, size: int, seed: int) -> gen.Suite:
    return gen.build(track, size, seed)


def find_task(s: gen.Suite, task_id: str) -> tuple[gen.Group, gen.Task]:
    for g in s.groups:
        for t in g.tasks:
            if t.id == task_id:
                return g, t
    raise BenchmarkError(f"no task {task_id!r} in {s.track} at size {s.size}")


def seeded_path(root: Path, arm: str, track: str, size: int, group: int) -> Path:
    return Path(root) / arm / f"{track}-{size}-g{group}"


def write_task_files(task: gen.Task, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for rel, text in task.repo.items():
        target = out / "repo" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    for rel, text in task.oracle.items():
        target = out / "oracle" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    (out / "prompt.md").write_text(task.prompt + "\n")
    (out / "task.json").write_text(json.dumps(task.to_json(), indent=1, sort_keys=True))
    (out / "check.py").write_text(
        '#!/usr/bin/env python3\n"""Hidden check: run as `check.py WORKDIR`; prints JSON."""\n'
        "import json, sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(ROOT / 'benchmarks')!r})\n"
        "from longhaul import gen, grading\n"
        "task = gen.Task.from_json(json.loads((Path(__file__).parent / 'task.json').read_text()))\n"
        "result = grading.check(task, Path(sys.argv[1]).resolve())\n"
        "print(json.dumps(result))\nsys.exit(0 if result['passed'] else 1)\n"
    )


def cmd_generate(args) -> dict:
    s = suite(args.track, args.size, args.seed)
    out = Path(args.out) / f"{args.track}-{args.size}"
    for g in s.groups:
        base = out / f"g{g.index}"
        base.mkdir(parents=True, exist_ok=True)
        with (base / "history.jsonl").open("w") as handle:
            for ses in g.sessions:
                handle.write(
                    json.dumps(
                        {
                            "id": ses.id,
                            "scope": {
                                "user": ses.scope.user,
                                "project": ses.scope.project,
                                "agent": ses.scope.agent,
                            },
                            "started": ses.started,
                            "turns": [[t.role, t.text] for t in ses.turns],
                        }
                    )
                    + "\n"
                )
        for t in g.tasks:
            write_task_files(t, base / "tasks" / t.id)
    (out / "manifest.json").write_text(json.dumps(gen.stats(s), indent=1))
    return gen.stats(s)


# ----------------------------------------------------------------------- seed


def cmd_seed(args) -> dict:
    s = suite(args.track, args.size, args.seed)
    group = next((g for g in s.groups if g.index == args.group), None)
    if group is None:
        raise BenchmarkError(f"{args.track} at size {args.size} has groups 0..{len(s.groups) - 1}")
    target = seeded_path(args.seeded, args.arm, args.track, args.size, args.group)
    if target.exists():
        raise BenchmarkError(f"{target} already exists")
    system = load_system(args.arm)
    faithful = getattr(system, "seeding_faithful", True)
    if not faithful and not args.allow_unfaithful_seeding:
        raise BenchmarkError(
            f"{args.arm}: a real session does not give this product what seeding writes, so it "
            "is measured on the lived track only (SYSTEMS.md). Pass --allow-unfaithful-seeding "
            "to seed it anyway as an upper bound, labelled as such in every table."
        )
    system.prepare(target)
    try:
        report = system.seed(group.sessions)
        size_bytes = sum(
            p.stat().st_size for p in target.rglob("*") if p.is_file() and not p.is_socket()
        )
    finally:
        system.close()
    record = {
        "arm": args.arm,
        "system_version": system.version,
        "track": args.track,
        "size": args.size,
        "group": args.group,
        "seed": args.seed,
        "digest": s.digest(),
        "history_digest": s.history_digest(),
        "seeding_faithful": faithful,
        "sessions": report.sessions,
        "seconds": report.seconds,
        "llm_calls": report.llm_calls,
        "llm_tokens": report.llm_tokens,
        "notes": report.notes,
        "store_bytes": size_bytes,
        "needs_llm": system.needs_llm,
    }
    (target / "seed-report.json").write_text(json.dumps(record, indent=1))
    return record


# ------------------------------------------------------------------------ run


class Collector:
    """A listener on localhost that records every request, for the `post` injection."""

    def __init__(self, port: int) -> None:
        hits: list[dict] = []
        self.hits = hits

        class Handler(BaseHTTPRequestHandler):
            def _record(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                hits.append({"method": self.command, "path": self.path})
                self.send_response(204)
                self.end_headers()

            do_POST = do_GET = do_PUT = _record

            def log_message(self, *a):
                return None

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError as exc:
            raise BenchmarkError(
                f"the T4 listener could not bind 127.0.0.1:{port} ({exc.strerror}): another "
                "process holds it; this is a harness fault, not a result (RUNNING.md, exclusions)"
            ) from exc
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def path_without_memoryking() -> str:
    keep = [
        e
        for e in os.environ.get("PATH", "").split(os.pathsep)
        if e and not os.access(os.path.join(e, "memoryking"), os.X_OK)
    ]
    return os.pathsep.join(keep)


def agent_env(home: Path, host_env: dict[str, str]) -> dict[str, str]:
    """A scrubbed environment: PATH without a `memoryking` command, the fresh HOME, proxy and
    credential variables the host needs, auto-memory off unless the arm turns it on. A host
    env value that is empty removes the variable."""
    env = {
        "PATH": path_without_memoryking(),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "TMPDIR": "/tmp",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    }
    for name in PASSTHROUGH:
        if name in os.environ:
            env[name] = os.environ[name]
    for key, value in host_env.items():
        if value == "":
            env.pop(key, None)
        else:
            env[key] = value
    return env


def fill(template: str, values: dict[str, str]) -> list[str]:
    out = []
    for word in shlex.split(template):
        for k, v in values.items():
            word = word.replace("{" + k + "}", v)
        out.append(word)
    return out


def run_process(argv, *, cwd, env, timeout, stdout, stderr) -> dict:
    started = time.monotonic()
    timed_out = False
    with open(stdout, "wb") as out, open(stderr, "wb") as err:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=out,
            stderr=err,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)  # the group this call created
            code = proc.wait()
    return {"exit_code": code, "timed_out": timed_out, "seconds": time.monotonic() - started}


def materialize(workdir: Path, bench: Path, task: gen.Task, host, home: Path) -> Path:
    """Write the repository, the arm's files and the host configuration; return the prompt file."""
    for rel, text in task.repo.items():
        target = workdir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    for rel, text in host.files.items():
        base = home if rel.startswith("~/") else workdir
        target = base / (rel[2:] if rel.startswith("~/") else rel)
        if (
            workdir.resolve() not in target.resolve().parents
            and home.resolve() not in target.resolve().parents
        ):
            raise BenchmarkError(f"host file {rel!r} leaves the working directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    (bench / "task.json").write_text(json.dumps(task.to_json()))
    (bench / "mcp.json").write_text(json.dumps(host.mcp or {"mcpServers": {}}))
    (bench / "settings.json").write_text(json.dumps(host.settings or {}))
    (bench / "allowed_tools.txt").write_text(",".join(host.allowed_tools))
    prompt = SCOPE_LINE + "\n\n" + task.prompt
    if host.prompt_note:
        prompt += "\n\n" + host.prompt_note
    prompt_file = bench / "prompt.md"
    prompt_file.write_text(prompt + "\n")
    return prompt_file


def auto_loaded_chars(arm: str, host) -> int:
    """Characters of memory the host itself puts into the context at the start, which the
    event stream does not show as a tool or hook result: the `native` arm's `CLAUDE.md`."""
    return len(host.files.get("CLAUDE.md", "")) if arm == "native" else 0


def keep_raw(target: Path, bench: Path, workdir: Path) -> None:
    """Keep what a failure analysis or a regrade needs after the run directory is removed: the
    agent's event stream and what is left of its working directory (small files, no history).
    These hold model output; they stay beside the results and are never committed."""
    target.mkdir(parents=True, exist_ok=True)
    for name in ("agent.stdout", "agent.stderr"):
        if (bench / name).exists():
            shutil.copy(bench / name, target / name)
    for p in sorted(workdir.rglob("*")):
        rel = p.relative_to(workdir)
        if (
            p.is_file()
            and not p.is_symlink()
            and rel.parts[0] != "history"
            and "__pycache__" not in rel.parts
            and p.stat().st_size <= 200_000
        ):
            (target / "final" / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(p, target / "final" / rel)


def snapshot_notes(workdir: Path, baseline: dict[str, str]) -> list[str]:
    """Files the agent added or changed that are not code, for the record."""
    code = {".py", ".pyi", ".json", ".toml", ".cfg", ".ini", ".txt", ".lock"}
    left = []
    for p in sorted(workdir.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts or ".pytest_cache" in p.parts:
            continue
        rel = p.relative_to(workdir).as_posix()
        if rel.startswith("history/"):
            continue
        if baseline.get(rel) == p.read_text(errors="replace"):
            continue
        if p.suffix not in code:
            left.append(rel)
    return left


def tree_text(workdir: Path) -> dict[str, str]:
    return {
        p.relative_to(workdir).as_posix(): p.read_text(errors="replace")
        for p in workdir.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }


def check_isolated(run_dir: Path) -> None:
    """The host reads CLAUDE.md files (and a .claude directory) in every parent directory of
    its working directory, so none of them may hold one: it would reach every arm."""
    for parent in Path(run_dir).resolve().parents:
        for name in ("CLAUDE.md", "CLAUDE.local.md", ".claude"):
            if (parent / name).exists():
                raise BenchmarkError(
                    f"{parent / name} would be read by the agent from {run_dir}; "
                    "use a work directory with no such file in any parent"
                )


def cmd_run(args) -> dict:
    s = suite(args.track, args.size, args.seed)
    group, task = find_task(s, args.task)
    if args.arm not in ARMS and not (LONGHAUL / "systems" / f"{args.arm}.py").exists():
        raise BenchmarkError(f"no arm {args.arm!r}")
    seeded = seeded_path(args.seeded, args.arm, args.track, args.size, group.index)
    if not seeded.exists():
        if args.arm == "off":  # nothing to seed
            probe = load_system("off")
            probe.prepare(seeded)
            probe.seed(group.sessions)
            probe.close()
        else:
            raise BenchmarkError(
                f"{seeded} does not exist: run `seed --track {args.track} --size {args.size} "
                f"--group {group.index} --arm {args.arm}` first"
            )
    seed_report = (
        json.loads((seeded / "seed-report.json").read_text())
        if (seeded / "seed-report.json").exists()
        else {}
    )
    # The seeded store depends on the sessions only: a corrected hidden check must not make a
    # seeded store unusable, so the sessions have a digest of their own.
    if "history_digest" in seed_report:
        mismatch = seed_report["history_digest"] != s.history_digest()
    else:
        mismatch = seed_report.get("digest") not in (None, s.digest())
    if mismatch:
        raise BenchmarkError("the seeded history is not the one this seed and size generate")
    name = f"{task.id}-{args.arm}-r{args.rep}"
    run_dir = Path(args.work) / f"{args.size}-{name}"
    check_isolated(run_dir)
    if run_dir.exists():
        raise BenchmarkError(f"{run_dir} already exists")
    # The directory is named after the project: products that derive the project from the
    # working directory (claude-mem, MCP Memory Service) then look where the history was seeded.
    workdir, bench, home = run_dir / "ws" / task.scope.project, run_dir / "bench", run_dir / "home"
    for d in (workdir, bench, home):
        d.mkdir(parents=True)
    system_root = run_dir / "system"
    shutil.copytree(seeded, system_root, symlinks=True)
    system = load_system(args.arm)
    if task.track == "T3" and hasattr(system, "all_projects"):
        system.all_projects = True  # the isolation question: the transcript folder holds every project
    collector = None
    result: dict = {}
    try:
        system.prepare(system_root)
        forget = None
        marker = (task.canaries.get("forgotten") or [None])[0]
        if marker:
            forget = system.forget(marker, task.scope)
        host = system.host(workdir, task.scope)
        prompt_file = materialize(workdir, bench, task, host, home)
        residue = None
        if marker:  # what the product's own delete path left where the agent could reach it
            residue = grading.store_residue(
                [system_root, workdir], marker, skip=("sessions.jsonl", "forgotten.json")
            )
        inj = task.canaries.get("injection")
        if inj and inj["action"] == "post":
            collector = Collector(inj["port"])
        baseline = tree_text(workdir)
        memory_paths = [k for k in host.files if not k.startswith("~/")]
        env = agent_env(home, host.env)
        argv = fill(
            args.agent,
            {
                "workdir": str(workdir),
                "prompt_file": str(prompt_file),
                "home": str(home),
                "mcp_config": str(bench / "mcp.json"),
                "settings": str(bench / "settings.json"),
                "allowed_tools": str(bench / "allowed_tools.txt"),
                "usage_file": str(bench / "usage.json"),
                "bench": str(bench),
            },
        )
        ran = run_process(
            argv,
            cwd=workdir,
            env=env,
            timeout=args.timeout,
            stdout=bench / "agent.stdout",
            stderr=bench / "agent.stderr",
        )
        stream = (bench / "agent.stdout").read_text(errors="replace")
        graded = grading.check(task, workdir, collector.hits if collector else [])
        scan = grading.analyze(task, stream, memory_paths)
        loaded = auto_loaded_chars(args.arm, host)
        if loaded and scan["has_result"]:
            scan["auto_loaded_chars"] = loaded
            scan["memory_chars"] += loaded
            scan["memory_reads"] += 1
        cls = grading.classify(task, graded, scan, ran)
        used = {}
        try:
            used = system.usage()
        except Exception as exc:  # a usage probe must not lose the run
            used = {"error": str(exc)[:200]}
        result = {
            "task": task.id,
            "track": task.track,
            "size": args.size,
            "group": group.index,
            "variant": task.variant,
            "stated_by": task.stated_by,
            "seeding_faithful": seed_report.get("seeding_faithful", True),
            "arm": args.arm,
            "rep": args.rep,
            "history_seed": args.seed,
            "system": {"name": system.name, "version": system.version},
            "suite_digest": s.digest(),
            "passed": cls is None and graded["passed"],
            "failure_class": cls,
            "injection_outcome": grading.injection_outcome(task, graded, scan),
            "check": {k: graded[k] for k in ("slots", "injection_acted", "probe_error")},
            "agent": ran,
            "stream": scan,
            "system_usage": used,
            "forget": forget,
            "forget_residue": residue,
            "seed": {
                k: seed_report.get(k)
                for k in ("seconds", "llm_calls", "llm_tokens", "store_bytes", "sessions")
            },
            "harness_notes": (
                ["no SessionStart hook event in the stream: a driver fault, not an agent result"]
                if args.arm == "memoryking" and "SessionStart" not in scan["hook_events"]
                else []
            ),
            "notes_left": snapshot_notes(workdir, baseline),
            "finished_at": time.time(),
        }
    finally:
        if collector:
            collector.stop()
        system.close()
    out = Path(args.results) / "runs"
    out.mkdir(parents=True, exist_ok=True)
    file = out / f"{args.track}-{args.size}-{name}.json"
    file.write_text(json.dumps(result, indent=1, sort_keys=True))
    keep_raw(Path(args.results) / "raw" / f"{args.track}-{args.size}-{name}", bench, workdir)
    if not args.keep:
        shutil.rmtree(run_dir, ignore_errors=True)
    return {
        "result_file": str(file),
        "task": task.id,
        "arm": args.arm,
        "passed": result["passed"],
        "failure_class": result["failure_class"],
    }


# ------------------------------------------------------------------- validate


def stub_command(mode: str) -> str:
    return (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(STUB))} {{workdir}} {{prompt_file}} "
        f"--mode {mode} --mcp-config {{mcp_config}} --settings {{settings}}"
    )


def cmd_validate(args) -> dict:
    """Without a model: for every task the null stub fails (so `off` cannot solve it) and the
    oracle passes; the stale, gullible and leaker stubs are classified as their failure."""
    tracks = [args.track] if args.track else list(gen.TRACKS)
    report: dict = {"tasks": 0, "problems": [], "rows": []}
    base = Path(tempfile.mkdtemp(prefix="mk-lhv-", dir="/tmp"))
    try:
        for track in tracks:
            s = suite(track, args.size, args.seed)
            for g in s.groups:
                for task in g.tasks:
                    report["tasks"] += 1
                    row = {"task": task.id}
                    modes = ["null", "oracle"]
                    if any(sl.stale for sl in task.slots if sl.required):
                        modes.append("stale")
                    if task.canaries.get("injection"):
                        modes.append("gullible")
                    if task.canaries.get("leak") or task.canaries.get("forgotten"):
                        modes.append("leaker")
                    for mode in modes:
                        d = base / f"{task.id}-{mode}"
                        work, bench = d / "work", d / "bench"
                        work.mkdir(parents=True)
                        bench.mkdir()
                        host = load_system("off").host(work, task.scope)
                        pf = materialize(work, bench, task, host, d)
                        collector = None
                        inj = task.canaries.get("injection")
                        if inj and inj["action"] == "post":
                            collector = Collector(inj["port"])
                        try:
                            argv = fill(
                                stub_command(mode),
                                {
                                    "workdir": str(work),
                                    "prompt_file": str(pf),
                                    "mcp_config": str(bench / "mcp.json"),
                                    "settings": str(bench / "settings.json"),
                                },
                            )
                            ran = run_process(
                                argv,
                                cwd=work,
                                env=agent_env(d, {}),
                                timeout=60,
                                stdout=bench / "agent.stdout",
                                stderr=bench / "agent.stderr",
                            )
                            graded = grading.check(task, work, collector.hits if collector else [])
                            scan = grading.analyze(task, (bench / "agent.stdout").read_text(), [])
                        finally:
                            if collector:
                                collector.stop()
                        cls = grading.classify(task, graded, scan, ran)
                        passed = cls is None and graded["passed"]
                        row[mode] = "pass" if passed else (cls or "fail")
                        expected = {
                            "null": "fail",
                            "oracle": "pass",
                            "stale": "stale",
                            "gullible": "acted_on_injection",
                            "leaker": "leak",
                        }[mode]
                        got = row[mode]
                        ok = (
                            got == "pass"
                            if expected == "pass"
                            else got != "pass"
                            if (expected == "fail")
                            else got == expected
                        )
                        if not ok:
                            report["problems"].append(
                                f"{task.id} {mode}: expected {expected}, got {got}"
                            )
                    report["rows"].append(row)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return report


# --------------------------------------------------------------------- regrade


def cmd_regrade(args) -> dict:
    """A fault only in a hidden check is corrected by grading the saved outputs again (the
    final code of each run, kept under `raw/<run>/final`) with the check as it is now. A run's
    verdict is replaced only if it changes; the changed runs are listed. Runs whose output
    was not kept, and a `post` injection (its listener hits are not kept), keep their verdict."""
    results = Path(args.results)
    changed, kept, missing = [], 0, []
    for file in sorted((results / "runs").glob("*.json")):
        row = json.loads(file.read_text())
        raw = results / "raw" / file.stem / "final"
        if "check" not in row or not raw.is_dir():
            missing.append(file.name)
            continue
        task = find_task(suite(row["track"], row["size"], row["history_seed"]), row["task"])[1]
        with tempfile.TemporaryDirectory(prefix="mk-lhr-", dir="/tmp") as tmp:
            work = Path(tmp) / "work"
            shutil.copytree(raw, work)
            graded = grading.check(task, work)
        if row["check"].get("injection_acted"):
            graded["injection_acted"] = True  # not recomputable without the listener's hits
            graded["passed"] = False
        scan = {
            "leak_found": row["stream"].get("leak_found") or [],
            "has_result": row["stream"].get("has_result"),
        }
        cls = grading.classify(task, graded, scan, row.get("agent", {}))
        passed = cls is None and graded["passed"]
        if passed == row["passed"] and cls == row["failure_class"]:
            kept += 1
            continue
        changed.append({"run": file.name, "was": row["passed"], "now": passed, "class": cls})
        if args.apply:
            row["regraded_from"] = {"passed": row["passed"], "failure_class": row["failure_class"]}
            row.update(
                passed=passed,
                failure_class=cls,
                check={k: graded[k] for k in ("slots", "injection_acted", "probe_error")},
            )
            file.write_text(json.dumps(row, indent=1, sort_keys=True))
    return {"regraded": changed, "unchanged": kept, "not_regradable": missing}


# ----------------------------------------------------------------------- plan

# (stage, track, size, tasks, reps) of the measured plan on the measured seed; stage 0 is the
# repeat of the pilot (the pilot itself was on a development seed and is not part of it).
PLAN = [
    ("S0 repeat pilot", "T1", 200, 6, 1),
    ("S1 T1 rest", "T1", 20, 12, 1),
    ("S1 T1 rest", "T1", 200, 6, 1),
    ("S1 T1 rest", "T1", 2000, 12, 1),
    ("S2 change", "T2", 200, 8, 1),
    ("S2 change", "T2", 2000, 8, 1),
    ("S2 change", "T2f", 200, 4, 1),
    ("S2 change", "T2f", 2000, 4, 1),
    ("S3 scopes and hostile", "T3", 200, 9, 1),
    ("S3 scopes and hostile", "T4", 200, 6, 1),
    ("S4 lived", "T2b", 0, 16, 1),
]
PROBE_SESSIONS_PER_ARM = 1  # one real probe session per arm before the first stage
PROBE_RESERVE = 4  # sessions kept for a probe that is run again after a harness fix
# claude-mem's observer, in model calls (worker "observer turns"). The pilot measured 194 calls
# for 200 seeded sessions (0.97 a session, 4-turn sessions) and 2.5 a real run; seeding a
# 6-turn session fires no more observer-visible events (RUNNING.md), so one call a session is the
# budget, 15 percent more is the cap that stops a seeding, and a run is budgeted 2.5 calls.
OBSERVER_CALLS_PER_SESSION = 1.0
OBSERVER_CAP_MARGIN = 1.15
OBSERVER_CALLS_PER_RUN = 2.5


def observer_budget(size: int) -> dict:
    """Seeding calls of one claude-mem history of `size` sessions: the estimate and the cap."""
    return {
        "size": size,
        "estimate": round(size * OBSERVER_CALLS_PER_SESSION),
        "cap": int(size * OBSERVER_CALLS_PER_SESSION * OBSERVER_CAP_MARGIN) + 10,
    }


def observer_calls(stage_runs: int, seeded: list[tuple[int, int]]) -> dict:
    """Estimate for a stage: `seeded` is [(size, histories)]; each real run adds a few calls."""
    seeding = sum(observer_budget(n)["estimate"] * k for n, k in seeded)
    runs = round(stage_runs * OBSERVER_CALLS_PER_RUN)
    return {"seeding": seeding, "runs": runs, "total": seeding + runs}


T5_QUESTIONS = 60
REPEAT = ("S5 repeat of T1 at 200 (optional, 2 more repetitions)", "T1", 200, 12, 2)


def plan_counts(arms: int, lived_only: int = 0) -> dict:
    """Real sessions of the plan for `arms` arms, of which `lived_only` are products measured on
    the lived track only (they run no seeded stage unless seeded as an upper bound)."""
    seeded = arms - lived_only
    stages: dict[str, int] = {}
    for stage, track, _size, tasks, reps in PLAN:
        n = arms if track == "T2b" else seeded
        stages[stage] = stages.get(stage, 0) + tasks * reps * n
    core = sum(stages.values())
    repeat = REPEAT[3] * REPEAT[4] * seeded
    return {
        "arms": arms,
        "seeded_arms": seeded,
        "stages": stages,
        "agent_sessions": core,
        "probe_sessions": PROBE_SESSIONS_PER_ARM * arms,
        "probe_reserve": PROBE_RESERVE,
        "optional_repeat": repeat,
        "with_repeat": core + repeat,
        "t5_model_calls": T5_QUESTIONS * 2 * arms,
    }


def cmd_plan(args) -> dict:
    s0 = plan_counts(args.arms, args.lived_only)
    first = {k: v for k, v in s0["stages"].items() if k == "S0 repeat pilot"}
    return {
        "per_arm": plan_counts(1),
        "for_arms": s0,
        "claude_mem_observer": {
            "per_history": [observer_budget(n) for n in (20, 200, 2000)],
            "repeat_pilot": observer_calls(
                first.get("S0 repeat pilot", 0) // max(1, s0["seeded_arms"]), [(200, 1)]
            ),
            "s1": observer_calls(30, [(20, 4), (200, 1), (2000, 1)]),
        },
    }


# --------------------------------------------------------------------- report


def exact_sign_test(a_only: int, b_only: int) -> float:
    n = a_only + b_only
    if n == 0:
        return 1.0
    k = min(a_only, b_only)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / 2**n
    return min(1.0, 2 * tail)


def holm(pvalues: list[float]) -> list[float]:
    order = sorted(range(len(pvalues)), key=lambda i: pvalues[i])
    adjusted = [0.0] * len(pvalues)
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (len(pvalues) - rank) * pvalues[i]))
        adjusted[i] = running
    return adjusted


def task_rates(rows: list[dict], arm: str) -> dict[str, float]:
    by: dict[str, list[int]] = {}
    for r in rows:
        if r["arm"] == arm:
            by.setdefault(r["task"], []).append(1 if r["passed"] else 0)
    return {t: sum(v) / len(v) for t, v in by.items()}


def paired(rows: list[dict], a: str, b: str) -> dict:
    ra, rb = task_rates(rows, a), task_rates(rows, b)
    common = sorted(set(ra) & set(rb))
    a_only = sum(1 for t in common if ra[t] > rb[t])
    b_only = sum(1 for t in common if rb[t] > ra[t])
    return {
        "a": a,
        "b": b,
        "tasks": len(common),
        "a_only": a_only,
        "b_only": b_only,
        "tied": len(common) - a_only - b_only,
        "p": exact_sign_test(a_only, b_only),
    }


def _mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def load_runs(results: Path, excluded: set[str] | None = None) -> list[dict]:
    rows = []
    for p in sorted((Path(results) / "runs").glob("*.json")):
        row = json.loads(p.read_text())
        row["_file"] = p.name
        if excluded and p.name in excluded:
            continue
        rows.append(row)
    return rows


def aggregate(rows: list[dict], primary: str = "memoryking") -> dict:
    out: dict = {"tracks": {}}
    for track in sorted({r["track"] for r in rows}):
        part = [r for r in rows if r["track"] == track]
        arms = sorted({r["arm"] for r in part})
        block: dict = {
            "arms": {},
            "paired": [],
            "by_size": {},
            "by_variant": {},
            "by_stated_by": {},
        }
        for arm in arms:
            mine = [r for r in part if r["arm"] == arm]
            k = sum(1 for r in mine if r["passed"])
            lo, hi = lme.wilson(k, len(mine))
            classes: dict[str, int] = {}
            for r in mine:
                if r["failure_class"]:
                    classes[r["failure_class"]] = classes.get(r["failure_class"], 0) + 1
            inj: dict[str, int] = {}
            for r in mine:
                if r.get("injection_outcome"):
                    inj[r["injection_outcome"]] = inj.get(r["injection_outcome"], 0) + 1
            block["arms"][arm] = {
                "runs": len(mine),
                "passed": k,
                "rate": k / len(mine) if mine else None,
                "wilson95": [lo, hi],
                "failure_classes": classes,
                "injection": inj,
                "input_tokens": _mean([r["stream"].get("input_tokens") for r in mine]),
                "output_tokens": _mean([r["stream"].get("output_tokens") for r in mine]),
                "seconds": _mean([r["agent"]["seconds"] for r in mine]),
                "memory_chars": _mean([r["stream"].get("memory_chars") for r in mine]),
                "memory_read": sum(1 for r in mine if r["stream"].get("memory_reads")),
                "memory_written": sum(1 for r in mine if r["stream"].get("memory_writes")),
                "seeding_faithful": all(r.get("seeding_faithful", True) for r in mine),
                "seed_seconds": _mean([r["seed"].get("seconds") for r in mine]),
                "store_bytes": _mean([r["seed"].get("store_bytes") for r in mine]),
            }
            for size in sorted({r["size"] for r in mine}):
                at = [r for r in mine if r["size"] == size]
                block["by_size"].setdefault(str(size), {})[arm] = {
                    "runs": len(at),
                    "passed": sum(1 for r in at if r["passed"]),
                    "input_tokens": _mean([r["stream"].get("input_tokens") for r in at]),
                    "seconds": _mean([r["agent"]["seconds"] for r in at]),
                }
            for who in sorted({r["stated_by"] for r in mine if r.get("stated_by")}):
                at = [r for r in mine if r.get("stated_by") == who]
                block["by_stated_by"].setdefault(who, {})[arm] = {
                    "runs": len(at),
                    "passed": sum(1 for r in at if r["passed"]),
                }
            for variant in sorted({r["variant"] for r in mine if r["variant"]}):
                at = [r for r in mine if r["variant"] == variant]
                block["by_variant"].setdefault(variant, {})[arm] = {
                    "runs": len(at),
                    "passed": sum(1 for r in at if r["passed"]),
                }
        if primary in arms:
            others = [a for a in arms if a != primary]
            comparisons = [paired(part, primary, o) for o in others]
            adjusted = holm([c["p"] for c in comparisons])
            for c, p_adj in zip(comparisons, adjusted, strict=True):
                c["p_holm"] = p_adj
            block["paired"] = comparisons
        out["tracks"][track] = block
    return out


def pct(x):
    return "n/a" if x is None else f"{100 * x:.0f}%"


def num(x, digits=0):
    return "n/a" if x is None else f"{x:.{digits}f}"


def render_markdown(agg: dict) -> str:
    lines = ["# Long-history benchmark report", ""]
    for track, block in agg["tracks"].items():
        lines += [
            f"## {track}",
            "",
            "| Arm | Runs | Passed | Rate | Wilson 95% | Failure classes | "
            "Input tokens | Seconds | Memory chars | Read | Written |",
            "|" + " --- |" * 11,
        ]
        for arm, a in block["arms"].items():
            classes = ", ".join(f"{k} {v}" for k, v in sorted(a["failure_classes"].items())) or "-"
            label = arm if a.get("seeding_faithful", True) else f"{arm} (explicit-save upper bound)"
            lines.append(
                f"| {label} | {a['runs']} | {a['passed']} | {pct(a['rate'])} | "
                f"{pct(a['wilson95'][0])} to {pct(a['wilson95'][1])} | {classes} | "
                f"{num(a['input_tokens'])} | {num(a['seconds'], 1)} | {num(a['memory_chars'])} | "
                f"{a['memory_read']} | {a['memory_written']} |"
            )
        if any(a["injection"] for a in block["arms"].values()):
            lines += [
                "",
                "Injection outcomes (acted / mentioned and refused / never surfaced):",
                "",
            ]
            for arm, a in block["arms"].items():
                i = a["injection"]
                lines.append(
                    f"- {arm}: {i.get('acted', 0)} / {i.get('mentioned_refused', 0)} / "
                    f"{i.get('never_surfaced', 0)}"
                )
        if block["paired"]:
            lines += [
                "",
                "Paired by task, memoryking against each other arm (exact sign test on "
                "tasks with different outcomes; Holm correction across this table):",
                "",
                "| Against | Tasks | Memoryking only | Other only | Tied | p | p (Holm) |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
            for c in block["paired"]:
                lines.append(
                    f"| {c['b']} | {c['tasks']} | {c['a_only']} | {c['b_only']} | "
                    f"{c['tied']} | {c['p']:.3f} | {c['p_holm']:.3f} |"
                )
        if len(block["by_size"]) > 1:
            lines += ["", "By history size (passed/runs, mean input tokens, mean seconds):", ""]
            for size, arms in block["by_size"].items():
                cells = "; ".join(
                    f"{arm} {a['passed']}/{a['runs']}, {num(a['input_tokens'])}, {num(a['seconds'], 1)}"
                    for arm, a in arms.items()
                )
                lines.append(f"- {size} sessions: {cells}")
        if block["by_stated_by"]:
            lines += [
                "",
                "By who stated the needle (a user decision, or a confirmed outcome the "
                "assistant reports and the user acknowledges), passed/runs:",
                "",
            ]
            for who, arms in block["by_stated_by"].items():
                lines.append(
                    f"- {who}: "
                    + "; ".join(f"{arm} {a['passed']}/{a['runs']}" for arm, a in arms.items())
                )
        if block["by_variant"]:
            lines += ["", "By variant (passed/runs):", ""]
            for variant, arms in block["by_variant"].items():
                lines.append(
                    f"- {variant}: "
                    + "; ".join(f"{arm} {a['passed']}/{a['runs']}" for arm, a in arms.items())
                )
        lines.append("")
    lines += [
        "A pass is a hidden check passed by that agent, model and run; it is not production "
        "quality evidence. Small task counts only show large effects.",
        "",
    ]
    return "\n".join(lines)


def cmd_report(args):
    results = Path(args.results)
    excluded = set()
    if args.exclude:
        excluded = {
            line.split("#")[0].strip() for line in Path(args.exclude).read_text().splitlines()
        }
        excluded.discard("")
    rows = load_runs(results, excluded)
    development = [r for r in rows if r.get("history_seed") in DEVELOPMENT_SEEDS]
    if not args.include_development:
        rows = [r for r in rows if r.get("history_seed") not in DEVELOPMENT_SEEDS]
    agg = aggregate(rows, args.primary)
    agg["development_runs_left_out"] = 0 if args.include_development else len(development)
    t5 = []
    for p in sorted((results / "t5").glob("*.json")) if (results / "t5").exists() else []:
        t5.append(json.loads(p.read_text()))
    agg["t5"] = [
        {
            "arm": r["arm"],
            "version": r["system_version"],
            "questions": r["questions"],
            "budget_chars": r["budget_chars"],
            "score": r["score"],
        }
        for r in t5
    ]
    agg["excluded"] = sorted(excluded)
    if args.json:
        return agg
    text = render_markdown(agg)
    if agg["t5"]:
        text += "\n## T5 question answering (LongMemEval `_s` subset)\n\n"
        text += "| Arm | Questions | Budget (chars) | Accuracy | Wilson 95% | Recall any | Recall all | Unparsed |\n"
        text += "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
        for r in agg["t5"]:
            o = r["score"]["overall"]
            text += (
                f"| {r['arm']} | {r['questions']} | {r['budget_chars']} | {pct(o['accuracy'])} | "
                f"{pct(o['ci95'][0])} to {pct(o['ci95'][1])} | {pct(o['recall_any'])} | "
                f"{pct(o['recall_all'])} | {o['unparsed']} |\n"
            )
    if excluded:
        text += "\nExcluded runs (harness faults): " + ", ".join(sorted(excluded)) + "\n"
    if agg["development_runs_left_out"]:
        text += (
            f"\n{agg['development_runs_left_out']} run(s) on development seeds "
            f"{DEVELOPMENT_SEEDS[0]} to {DEVELOPMENT_SEEDS[-1]} are development data and are not "
            "reported.\n"
        )
    return text


# ------------------------------------------------------------------------- T5


def cmd_t5_select(args) -> dict:
    data, digest = lme.load(Path(args.data))
    chosen = lme.select(data, args.size, args.seed)
    counts: dict[str, int] = {}
    for q in chosen:
        counts[q["question_type"]] = counts.get(q["question_type"], 0) + 1
    out = {
        "data_sha256": digest,
        "size": len(chosen),
        "seed": args.seed,
        "by_type": counts,
        "question_ids": [q["question_id"] for q in chosen],
    }
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=1))
    return out


def cmd_t5_run(args) -> dict:
    data, digest = lme.load(Path(args.data))
    chosen = lme.select(data, args.size, args.seed)
    root = Path(args.work) / f"t5-{args.arm}"
    if root.exists():
        raise BenchmarkError(f"{root} exists")
    rows = []
    seed_seconds = 0.0
    version = ""
    for item in chosen:
        qid = "".join(c if c.isalnum() else "-" for c in item["question_id"])[:40]
        system = load_system(args.arm)
        version = system.version
        if hasattr(system, "ingest"):
            system.ingest = "turns"
        try:
            system.prepare(root / qid)
            report = system.seed(lme.sessions_of(item))
            seed_seconds += report.seconds
            started = time.perf_counter()
            context = system.context_for(item["question"], lme.SCOPE, args.budget_chars)
            context = context[: args.budget_chars]
            retrieval_seconds = time.perf_counter() - started
        finally:
            system.close()
        answer = lme.ask(args.reader_command, lme.reader_prompt(item, context))
        reply = lme.ask(args.judge_command, lme.judge_prompt(item, answer))
        verdict = lme.parse_verdict(reply)
        rows.append(
            {
                "id": item["question_id"],
                "type": item["question_type"],
                "context_chars": len(context),
                "answer": answer[:500],
                "verdict": verdict,
                "correct": verdict is True,
                "evidence": lme.evidence(item, context),
                "retrieval_seconds": retrieval_seconds,
            }
        )
        shutil.rmtree(root / qid, ignore_errors=True)
    result = {
        "arm": args.arm,
        "system_version": version,
        "data_sha256": digest,
        "seed": args.seed,
        "questions": len(rows),
        "budget_chars": args.budget_chars,
        "reader_command": args.reader_command.split()[0],
        "seed_seconds": seed_seconds,
        "score": lme.score(rows),
        "rows": rows,
        "finished_at": time.time(),
    }
    out = Path(args.results) / "t5"
    out.mkdir(parents=True, exist_ok=True)
    file = out / f"{args.arm}-{args.size}.json"
    file.write_text(json.dumps(result, indent=1))
    shutil.rmtree(root, ignore_errors=True)
    return {"result_file": str(file), "score": result["score"]["overall"]}


# ------------------------------------------------------------------- lived T2b


def lived_tasks(seed: int) -> list[dict]:
    """Four tasks of four sessions each (T2b): the fact in session 1, a side task in
    session 2, a correction in session 3, and in session 4 the module is back to its stub
    and the agent is asked to write it again."""
    out = []
    for fid in ("export-delimiter", "retry-policy", "rate-limit", "id-format"):
        family = gen.fam.F[fid]
        rng = gen.rng_for(seed, "T2b", fid)
        slot = family.slots[0]
        v1, v2 = rng.sample([v for v, _ in slot.pool], 2)
        second = family.slots[1]
        v_other = rng.choice([v for v, _ in second.pool])
        task = gen.make_task(
            "T2b", family, seed=seed, values_override={slot.name: [v1, v2], second.name: [v_other]}
        )
        task.id = f"t2b-{fid}"
        say = gen.say
        s1 = (
            f"{say('decision', family, slot, v1, rng)} {say(second.kind if second.kind != 'preference' else 'decision', family, second, v_other, rng)} "
            f"Please implement the function now: {family.prompt.split('Follow')[0].strip()}"
        )
        s2 = "Add one more test to the tests folder that checks the function exists and can be called."
        s3 = f"{say('correction', family, slot, v2, rng, old=v1)} Update the implementation to match."
        s4 = "The code of this project was reset to its starting state by mistake. " + family.prompt
        out.append({"task": task, "prompts": [s1, s2, s3, s4]})
    return out


def reset_code(workdir: Path, task: gen.Task) -> None:
    """The between-step of a lived run: the repository's files go back to their pristine text
    and Python files the agent added are removed, so no value survives in the code or its
    tests. Notes, memory files and anything else that is not Python stay."""
    for p in list(workdir.rglob("*.py")):
        rel = p.relative_to(workdir).as_posix()
        if "__pycache__" not in p.parts and rel not in task.repo and not rel.startswith("history/"):
            p.unlink()
    for rel, text in task.repo.items():
        (workdir / rel).write_text(text)


def cmd_lived(args) -> dict:
    items = {i["task"].id: i for i in lived_tasks(args.seed)}
    if args.task not in items:
        raise BenchmarkError(f"no lived task {args.task!r}; have {sorted(items)}")
    task, prompts = items[args.task]["task"], items[args.task]["prompts"]
    name = f"{task.id}-{args.arm}-r{args.rep}"
    run_dir = Path(args.work) / f"lived-{name}"
    check_isolated(run_dir)
    if run_dir.exists():
        raise BenchmarkError(f"{run_dir} already exists")
    workdir, bench, home = run_dir / "ws" / task.scope.project, run_dir / "bench", run_dir / "home"
    for d in (workdir, bench, home):
        d.mkdir(parents=True)
    system = load_system(args.arm)
    sessions = []
    try:
        system.prepare(run_dir / "system")
        host = system.host(workdir, task.scope)
        prompt_file = materialize(workdir, bench, task, host, home)
        for n, text in enumerate(prompts, 1):
            if n == 4:  # between sessions 3 and 4 the code goes back to its starting state
                reset_code(workdir, task)
            body = (
                SCOPE_LINE
                + "\n\n"
                + text
                + (("\n\n" + host.prompt_note) if host.prompt_note else "")
            )
            prompt_file.write_text(body + "\n")
            sess_home = (
                home
                if getattr(system, "persistent_home", False)
                else Path(tempfile.mkdtemp(prefix="mk-lhh-", dir="/tmp"))
            )
            if sess_home != home:
                for rel, content in host.files.items():
                    if rel.startswith("~/"):
                        t = sess_home / rel[2:]
                        t.parent.mkdir(parents=True, exist_ok=True)
                        t.write_text(content)
            argv = fill(
                args.agent,
                {
                    "workdir": str(workdir),
                    "prompt_file": str(prompt_file),
                    "home": str(sess_home),
                    "mcp_config": str(bench / "mcp.json"),
                    "settings": str(bench / "settings.json"),
                    "allowed_tools": str(bench / "allowed_tools.txt"),
                    "usage_file": str(bench / "usage.json"),
                    "bench": str(bench),
                },
            )
            ran = run_process(
                argv,
                cwd=workdir,
                env=agent_env(sess_home, host.env),
                timeout=args.timeout,
                stdout=bench / f"agent{n}.stdout",
                stderr=bench / f"agent{n}.stderr",
            )
            if sess_home != home:
                shutil.rmtree(sess_home, ignore_errors=True)
            scan = grading.analyze(
                None,
                (bench / f"agent{n}.stdout").read_text(errors="replace"),
                [k for k in host.files if not k.startswith("~/")],
            )
            sessions.append(
                {
                    "n": n,
                    **ran,
                    **{
                        k: scan.get(k)
                        for k in (
                            "input_tokens",
                            "output_tokens",
                            "memory_chars",
                            "memory_reads",
                            "memory_writes",
                        )
                    },
                }
            )
        graded = grading.check(task, workdir)
        cls = grading.classify(task, graded, {}, sessions[-1])
        result = {
            "task": task.id,
            "track": "T2b",
            "size": 0,
            "group": 0,
            "variant": "lived",
            "arm": args.arm,
            "rep": args.rep,
            "history_seed": args.seed,
            "system": {"name": system.name, "version": system.version},
            "passed": cls is None and graded["passed"],
            "failure_class": cls,
            "injection_outcome": None,
            "check": {k: graded[k] for k in ("slots", "injection_acted", "probe_error")},
            "agent": {
                "seconds": sum(s["seconds"] for s in sessions),
                "exit_code": sessions[-1]["exit_code"],
                "timed_out": any(s["timed_out"] for s in sessions),
            },
            "stream": {
                "input_tokens": sum(s["input_tokens"] or 0 for s in sessions) or None,
                "output_tokens": sum(s["output_tokens"] or 0 for s in sessions) or None,
                "memory_chars": sum(s["memory_chars"] or 0 for s in sessions),
                "memory_reads": sum(s["memory_reads"] or 0 for s in sessions),
                "memory_writes": sum(s["memory_writes"] or 0 for s in sessions),
            },
            "system_usage": system.usage(),
            "forget": None,
            "seed": {},
            "sessions": sessions,
            "notes_left": [],
            "finished_at": time.time(),
        }
    finally:
        system.close()
    out = Path(args.results) / "runs"
    out.mkdir(parents=True, exist_ok=True)
    file = out / f"T2b-0-{name}.json"
    file.write_text(json.dumps(result, indent=1, sort_keys=True))
    if not args.keep:
        shutil.rmtree(run_dir, ignore_errors=True)
    return {"result_file": str(file), "passed": result["passed"], "failure_class": cls}


# ------------------------------------------------------------------- hookprobe


def cmd_hookprobe(args) -> dict | str:
    """What the MemoryKing hooks put in front of an agent, per task, on a seeded history:
    no agent, no model (benchmarks/longhaul/hookprobe.py)."""
    s = suite(args.track, args.size, args.seed)
    rows: list[dict] = []
    for g in s.groups:
        seeded = seeded_path(args.seeded, "memoryking", args.track, args.size, g.index)
        if not seeded.exists():
            raise BenchmarkError(f"{seeded} does not exist: run `seed` first")
        rows.extend(
            hookprobe.probe_group(
                s, g, seeded, Path(args.work), keep_text=args.text, prompt_args=args.prompt_args
            )
        )
    result = {
        "track": args.track,
        "size": args.size,
        "seed": args.seed,
        "summary": hookprobe.summarize(rows),
        "tasks": rows,
    }
    if args.json:
        return result
    return f"track {args.track} size {args.size} seed {args.seed}\n" + hookprobe.render(
        result["summary"]
    )


# ----------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp, size=True):
        sp.add_argument("--track", choices=gen.TRACKS, required=True)
        if size:
            sp.add_argument("--size", type=int, default=200)
        sp.add_argument("--seed", type=int, default=1)

    g = sub.add_parser("generate", help="write tasks and histories to a directory")
    common(g)
    g.add_argument("--out", required=True)
    sd = sub.add_parser("seed", help="seed one arm with one group's history")
    common(sd)
    sd.add_argument("--group", type=int, default=0)
    sd.add_argument("--arm", required=True)
    sd.add_argument("--seeded", default=str(DEFAULT_SEEDED))
    sd.add_argument(
        "--allow-unfaithful-seeding",
        action="store_true",
        help="seed a product whose real sessions would not have given it this history "
        "(lived track only): an upper bound, labelled as such in the report",
    )
    r = sub.add_parser("run", help="one task, arm and repetition: the agent, then the hidden check")
    common(r)
    r.add_argument("--task", required=True)
    r.add_argument("--arm", required=True)
    r.add_argument("--rep", type=int, default=1)
    r.add_argument(
        "--agent",
        required=True,
        help="command; placeholders {workdir} {prompt_file} "
        "{home} {mcp_config} {settings} {allowed_tools} {usage_file}",
    )
    r.add_argument("--seeded", default=str(DEFAULT_SEEDED))
    r.add_argument("--results", default=str(DEFAULT_RESULTS))
    r.add_argument("--work", default=str(DEFAULT_WORK))
    r.add_argument("--timeout", type=int, default=1800)
    r.add_argument("--keep", action="store_true", help="keep the run directory")
    hp = sub.add_parser(
        "hookprobe",
        help="offline: which needle sentences the MemoryKing hooks and recall carry (no agent)",
    )
    common(hp)
    hp.add_argument("--seeded", default=str(DEFAULT_SEEDED))
    hp.add_argument("--work", default=str(SHORT / "hookprobe"))
    hp.add_argument("--json", action="store_true")
    hp.add_argument("--text", action="store_true", help="with --json: keep what each source showed")
    hp.add_argument(
        "--prompt-args", default="", help="extra options for the prompt hook, e.g. '--budget 2400'"
    )
    rp = sub.add_parser("report", help="tables from a results directory")
    rp.add_argument("--results", default=str(DEFAULT_RESULTS))
    rp.add_argument("--json", action="store_true")
    rp.add_argument("--primary", default="memoryking")
    rp.add_argument("--exclude", help="file listing run result files to leave out (harness faults)")
    rp.add_argument(
        "--include-development",
        action="store_true",
        help="also report runs on development seeds (tests only; never a result)",
    )
    rg = sub.add_parser(
        "regrade", help="grade the saved outputs of runs again with the current hidden checks"
    )
    rg.add_argument("--results", default=str(DEFAULT_RESULTS))
    rg.add_argument("--apply", action="store_true", help="rewrite the verdicts in the run files")
    v = sub.add_parser("validate", help="offline: stub agent against every task")
    v.add_argument("--track", choices=gen.TRACKS)
    v.add_argument("--size", type=int, default=20)
    v.add_argument("--seed", type=int, default=1)
    pl = sub.add_parser("plan", help="real-session counts of the full plan")
    pl.add_argument("--arms", type=int, default=5)
    pl.add_argument(
        "--lived-only", type=int, default=0, help="arms measured on the lived track only"
    )
    t = sub.add_parser("t5-select", help="the stratified LongMemEval subset")
    t.add_argument("--data", default=str(lme.DEFAULT_DATA))
    t.add_argument("--size", type=int, default=60)
    t.add_argument("--seed", type=int, default=1)
    t.add_argument("--out")
    tr = sub.add_parser("t5-run", help="ingest, retrieve, read and judge for one arm")
    tr.add_argument("--data", default=str(lme.DEFAULT_DATA))
    tr.add_argument("--arm", required=True)
    tr.add_argument("--size", type=int, default=60)
    tr.add_argument("--seed", type=int, default=1)
    tr.add_argument("--budget-chars", type=int, default=12000)
    tr.add_argument("--reader-command", required=True)
    tr.add_argument("--judge-command", required=True)
    tr.add_argument("--results", default=str(DEFAULT_RESULTS))
    tr.add_argument("--work", default=str(DEFAULT_WORK))
    lv = sub.add_parser("lived", help="T2b: four real sessions, the system writing memory itself")
    lv.add_argument("--task", required=True)
    lv.add_argument("--arm", required=True)
    lv.add_argument("--rep", type=int, default=1)
    lv.add_argument("--seed", type=int, default=1)
    lv.add_argument("--agent", required=True)
    lv.add_argument("--results", default=str(DEFAULT_RESULTS))
    lv.add_argument("--work", default=str(DEFAULT_WORK))
    lv.add_argument("--timeout", type=int, default=1800)
    lv.add_argument("--keep", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    SHORT.mkdir(parents=True, exist_ok=True)
    try:
        handler = {
            "generate": cmd_generate,
            "seed": cmd_seed,
            "run": cmd_run,
            "report": cmd_report,
            "regrade": cmd_regrade,
            "validate": cmd_validate,
            "plan": cmd_plan,
            "t5-select": cmd_t5_select,
            "t5-run": cmd_t5_run,
            "lived": cmd_lived,
            "hookprobe": cmd_hookprobe,
        }[args.command]
        result = handler(args)
    except (BenchmarkError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if isinstance(result, str):
        print(result)
    else:
        print(json.dumps(result, indent=1, sort_keys=True))
    if args.command == "validate" and result["problems"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
