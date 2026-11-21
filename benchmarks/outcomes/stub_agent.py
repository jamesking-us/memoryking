#!/usr/bin/env python3
"""A deterministic stand-in for a coding agent, used to test the benchmark offline.

It calls no model. It reads the task's meta.json and reference solutions, which a real
agent never sees, and behaves like an agent that works faithfully:

* Session 1 applies the reference first-session work. It also saves what the task says
  session 1 learns: arm `on` with the public `memoryking capture` and `save-progress`
  commands, arm `mcp` by speaking MCP over stdio to the bridge named in `--mcp-config`
  (initialize, tools/list, tools/call remember and save_progress), arm `notes` by writing
  NOTES.md, arm `off` not at all.
* Session 2 asks for what it needs: `memoryking recall` and `resume`, the MCP tools
  `recall` and `resume`, or a read of NOTES.md. When the answer holds the facts the task
  says must carry over, it applies the reference solution; otherwise (arm `off`, or nothing
  useful recalled) it applies the plausible wrong one.

    stub_agent.py WORKDIR PROMPT_FILE [--usage-file F] [--mcp-config F] [--force solve|fail]

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
        payload = json.loads(result["content"][0]["text"])
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
    if bool(args.mcp_config) != (arm == "mcp"):
        raise SystemExit("--mcp-config does not match the arm")
    assert prompt.read_text().strip(), "empty prompt"
    progress = f"Progress: first part of the work is done in the repository ({meta['title']})."
    task = "stub-" + state["task"]

    if session == 1:
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
