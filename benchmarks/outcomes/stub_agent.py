#!/usr/bin/env python3
"""A deterministic stand-in for a coding agent, used to test the benchmark offline.

It calls no model. It reads the task's meta.json and reference solutions, which a real
agent never sees, and behaves like an agent that works faithfully:

* Session 1 applies the reference first-session work. It also saves what the task says
  session 1 learns: arm `on` with the public `memoryking capture` and `save-progress`
  commands, arm `mcp` by speaking MCP over stdio to the bridge named in `--mcp-config`
  (initialize, tools/list, tools/call remember and save_progress), arm `notes` by writing
  NOTES.md, arm `off` not at all.
* Arm `setup` is an agent that never decides to save or look anything up: the host's
  hooks do it. The stub runs the hook commands in the settings file `--settings` names, as
  a host does (event JSON on stdin): session start (its output is what the model would be
  given), then, after writing a transcript of the session in Claude Code's JSONL shape,
  session end; it also lists the bridge's tools and saves a progress note through it. In
  session 2 the briefing the start hook printed is all it has.
* Session 2 asks for what it needs: `memoryking recall` and `resume`, the MCP tools
  `recall` and `resume`, or a read of NOTES.md. When the answer holds the facts the task
  says must carry over, it applies the reference solution; otherwise (arm `off`, or nothing
  useful recalled) it applies the plausible wrong one.

    stub_agent.py WORKDIR PROMPT_FILE [--usage-file F] [--mcp-config F] [--settings F]
                  [--force solve|fail]

`--force solve` or `--force fail` skips the memory and decides the outcome directly, which
is how the tests exercise the report.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / "benchmarks" / "outcomes" / "tasks"
PROJECT = "work"


def overlay(source: Path, workdir: Path) -> None:
    if not source.is_dir():
        return
    shutil.copytree(source, workdir, dirs_exist_ok=True)
    marker = workdir / "_delete"
    if marker.exists():
        marker.unlink()
    delete = source / "_delete"
    if delete.is_file():
        for name in delete.read_text().split():
            target = workdir / name
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()


def run_hooks(settings: Path, event: str, payload: dict, matching: str | None = None) -> str:
    """Run the commands a host settings file names for `event`, as a host does: through a
    shell, the event as JSON on standard input, its own environment. Returns their stdout."""
    groups = json.loads(settings.read_text()).get("hooks", {}).get(event, [])
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
    printed = []
    for group in groups:
        matcher = group.get("matcher")
        if matcher and matching and not re.fullmatch(matcher, matching):
            continue
        for hook in group["hooks"]:
            proc = subprocess.run(
                hook["command"],
                shell=True,
                input=json.dumps({"hook_event_name": event, **payload}),
                capture_output=True,
                text=True,
                timeout=hook.get("timeout", 30),
                env=env,
            )
            if proc.returncode != 0:
                raise SystemExit(f"hook {event} failed: {proc.stderr.strip()[:200]}")
            printed.append(proc.stdout)
    return "".join(printed)


def transcript_lines(prompt: str, closing: str) -> str:
    """A session as Claude Code writes it: the prompt, one tool call, the closing message."""
    lines = [
        {"type": "user", "message": {"role": "user", "content": prompt}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I'll start by looking at the repository."},
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "README.md"}],
            },
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": closing}]},
        },
    ]
    return "".join(json.dumps(line) + "\n" for line in lines)


def memory(*args: str) -> str:
    """Run the `memoryking` command exactly as an agent would: from PATH, no paths."""
    proc = subprocess.run(["memoryking", *args], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise SystemExit(f"memoryking {args[0]} failed: {proc.stderr.strip()[:200]}")
    return proc.stdout


class McpBridge:
    """A minimal MCP client over stdio: launches the server a host configuration names."""

    def __init__(self, config_path: Path):
        server = json.loads(config_path.read_text())["mcpServers"]["memoryking"]
        env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
        env.update(server.get("env", {}))
        self.proc = subprocess.Popen(
            [server["command"], *server.get("args", [])],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
        )
        self.next_id = 0
        self.request(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "stub-agent", "version": "1"},
            },
        )
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict) -> dict:
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        line = self.proc.stdout.readline()
        if not line:
            raise SystemExit("the MCP server closed the connection")
        reply = json.loads(line)
        if "error" in reply:
            raise SystemExit(f"MCP {method} failed: {reply['error']}")
        return reply["result"]

    def tool_names(self) -> list[str]:
        return [t["name"] for t in self.request("tools/list", {})["tools"]]

    def call(self, name: str, arguments: dict) -> dict:
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        text = result["content"][0]["text"]
        # Lean replies: the envelope is `structuredContent`, a briefing's Markdown the text.
        payload = result.get("structuredContent")
        if payload is None:
            payload = json.loads(text)
        elif payload.get("format") == "text":
            payload = {**payload, "text": text}
        if result.get("isError"):
            raise SystemExit(f"MCP tool {name} failed: {payload}")
        return payload

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=30)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workdir")
    parser.add_argument("prompt_file")
    parser.add_argument("--usage-file")
    parser.add_argument("--mcp-config")
    parser.add_argument("--settings")
    parser.add_argument("--force", choices=("solve", "fail"))
    args = parser.parse_args()
    workdir = Path(args.workdir).resolve()
    prompt = Path(args.prompt_file)
    session = int(re.search(r"session(\d)", prompt.name).group(1))
    state = json.loads((workdir.parent / (workdir.name + ".bench") / "state.json").read_text())
    task_dir = TASKS / state["task"]
    meta = json.loads((task_dir / "meta.json").read_text())
    stub = meta["stub"]
    arm = state["arm"]
    if (workdir / "MEMORY.md").is_file() != (arm == "on"):
        raise SystemExit("MEMORY.md does not match the arm")
    if (workdir / "NOTES.md").is_file() != (arm == "notes") and session == 1:
        raise SystemExit("NOTES.md does not match the arm")
    if bool(args.mcp_config) != (arm in ("mcp", "setup")):
        raise SystemExit("--mcp-config does not match the arm")
    if bool(args.settings) != (arm == "setup"):
        raise SystemExit("--settings does not match the arm")
    assert prompt.read_text().strip(), "empty prompt"
    progress = f"Progress: first part of the work is done in the repository ({meta['title']})."
    task = "stub-" + state["task"]

    bench = workdir.parent / (workdir.name + ".bench")
    if arm == "setup":
        settings = Path(args.settings)
        host = {"session_id": f"stub-s{session}", "cwd": str(workdir)}
        started = run_hooks(
            settings, "SessionStart", {**host, "source": "startup", "prompt_id": "p"}, "startup"
        )
        (bench / f"context{session}.md").write_text(started)
        assert "mcp__memoryking__remember" in started, "the start hook did not name the tools"
        bridge = McpBridge(Path(args.mcp_config))
        listed = {t["name"]: t for t in bridge.request("tools/list", {})["tools"]}
        for needed in ("remember", "recall", "save_progress", "resume"):
            assert listed[needed]["_meta"]["anthropic/alwaysLoad"] is True, needed
        if session == 1:
            overlay(task_dir / "reference" / "s1", workdir)
            closing = " ".join([progress, *stub["learn"]])
            transcript = bench / "transcript1.jsonl"
            transcript.write_text(
                transcript_lines(prompt.read_text().strip(), closing)
            )
            bridge.call("remember", {"text": progress, "source": "progress"})
            bridge.call(
                "save_progress",
                {"task": task, "objective": meta["title"], "current_state": progress},
            )
            bridge.close()
            run_hooks(
                settings,
                "SessionEnd",
                {**host, "transcript_path": str(transcript), "reason": "other"},
            )
        else:
            bridge.close()
            informed = (
                args.force == "solve"
                if args.force
                else all(m.casefold() in started.casefold() for m in stub["must_recall"])
            )
            overlay(task_dir / ("reference" if informed else "reference_wrong") / "s2", workdir)
    elif session == 1:
        overlay(task_dir / "reference" / "s1", workdir)
        if arm == "on":
            for note in stub["learn"]:
                memory("capture", "--project", PROJECT, "--source", "agent", "--text", note)
            memory("capture", "--project", PROJECT, "--source", "progress", "--text", progress)
            memory(
                "save-progress",
                "--project",
                PROJECT,
                "--task",
                task,
                "--objective",
                meta["title"],
                "--state",
                progress,
            )
        elif arm == "mcp":
            bridge = McpBridge(Path(args.mcp_config))
            names = bridge.tool_names()
            for needed in ("remember", "recall", "save_progress", "resume"):
                assert needed in names, (needed, names)
            for note in stub["learn"]:
                bridge.call("remember", {"text": note})
            bridge.call("remember", {"text": progress, "source": "progress"})
            bridge.call(
                "save_progress",
                {"task": task, "objective": meta["title"], "current_state": progress},
            )
            bridge.close()
        elif arm == "notes":
            (workdir / "NOTES.md").write_text(
                "# Notes\n\n" + "\n".join(f"- {note}" for note in stub["learn"]) + "\n"
            )
    else:
        informed = False
        if args.force:
            informed = args.force == "solve"
        elif arm == "on":
            memory("resume", "--project", PROJECT, "--task", task)
            briefing = memory("recall", "--project", PROJECT, "--query", stub["recall_query"])
            informed = all(m.casefold() in briefing.casefold() for m in stub["must_recall"])
        elif arm == "mcp":
            bridge = McpBridge(Path(args.mcp_config))
            bridge.call("resume", {"task": task})
            briefing = bridge.call("recall", {"query": stub["recall_query"]})["text"]
            bridge.close()
            informed = all(m.casefold() in briefing.casefold() for m in stub["must_recall"])
        elif arm == "notes":
            notes = workdir / "NOTES.md"
            text = notes.read_text() if notes.is_file() else ""
            informed = all(m.casefold() in text.casefold() for m in stub["must_recall"])
        overlay(task_dir / ("reference" if informed else "reference_wrong") / "s2", workdir)

    if args.usage_file:
        # Synthetic figures so the report's token columns are exercised; not a measurement.
        Path(args.usage_file).write_text(
            json.dumps({"input_tokens": 1000 * session, "output_tokens": 100 * session})
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
