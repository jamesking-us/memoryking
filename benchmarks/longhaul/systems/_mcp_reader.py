"""What the offline `reader` stub does for the other products' arms: start each MCP server the
arm's configuration names, call its search tool the way a careful agent would, and return the
text. It exists only for the stub; a real agent uses the same tools itself.

Which tool is the search tool, and its arguments, is a table of the products' own tool names.
Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path


def _expand(value: str) -> str:
    return re.sub(
        r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: os.environ.get(m.group(1), m.group(0)), value
    )


class _Client:
    def __init__(self, entry: dict):
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "HOME", "TMPDIR")}
        env.update({k: _expand(str(v)) for k, v in (entry.get("env") or {}).items()})
        self.proc = subprocess.Popen(
            [entry["command"], *entry.get("args", [])],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._id = 0
        self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "stub", "version": "1"},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, obj: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        assert self.proc.stdout is not None
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("MCP server closed")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                return msg["result"]

    def call(self, name: str, args: dict) -> str:
        res = self.request("tools/call", {"name": name, "arguments": args})
        return "\n".join(
            c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"
        )

    def tools(self) -> set[str]:
        return {t["name"] for t in self.request("tools/list")["tools"]}

    def close(self) -> None:
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)  # the group this client created
        except (ProcessLookupError, PermissionError):
            pass
        deadline = time.monotonic() + 5
        while self.proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGKILL)
        for f in (self.proc.stdin, self.proc.stdout):
            if f:
                f.close()
        self.proc.wait()


def _search_calls(tools: set[str], query: str, scope: dict) -> list[tuple[str, dict]]:
    word = query.split()[0].lower()
    if "search_nodes" in tools and "create_entities" in tools:  # the Knowledge Graph Memory server
        return [("search_nodes", {"query": word})]
    if "bm_search" in tools:
        return [
            (
                "bm_search",
                {"query": query, "project": "main", "page_size": 20, "output_format": "json"},
            )
        ]
    if "memory_search" in tools:
        return [("memory_search", {"query": query})]
    if "search_memories" in tools:
        return [("search_memories", {"query": query, "limit": 20})]
    if "search_memory_facts" in tools:
        g = [scope["project"]]
        return [
            ("search_memory_facts", {"query": query, "group_ids": g, "max_facts": 20}),
            ("search_nodes", {"query": query, "group_ids": g, "max_nodes": 10}),
        ]
    if "search" in tools and "get_observations" in tools:  # claude-mem
        return [("search", {"query": query, "limit": 20, "project": scope["project"]})]
    return []


def read(mcp_config: Path, queries: list[str], scope: dict) -> tuple[str, str]:
    """(tool name, text) for the first non-MemoryKing server in the configuration that has a
    known search tool; one search per query, and for claude-mem the full records of its top hits."""
    servers = json.loads(Path(mcp_config).read_text()).get("mcpServers", {})
    for server, entry in servers.items():
        if server == "memoryking":
            continue
        client = _Client(entry)
        try:
            tools = client.tools()
            blocks: list[str] = []
            used = ""
            for q in queries:
                for tool, args in _search_calls(tools, q, scope):
                    used = used or f"mcp__{server}__{tool}"
                    text = client.call(tool, args)
                    blocks.append(text)
                    if tool == "search" and "get_observations" in tools:
                        ids = [int(i) for i in re.findall(r"#(\d+)\b", text)][:8]
                        if ids:
                            blocks.append(client.call("get_observations", {"ids": ids}))
            if used:
                return used, "\n".join(blocks)
        finally:
            client.close()
    return "none", ""
