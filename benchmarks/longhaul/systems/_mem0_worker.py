"""Runs inside the Mem0 virtualenv (it imports `mem0`). Two modes, one configuration:

  --rpc   newline-delimited JSON requests on stdin, one JSON answer per line on stdout; the
          adapter's ingest and retrieval path (`Memory.add`, `Memory.search`, `Memory.delete`).
  --mcp   a small MCP stdio server whose tools mirror the names of the project's own
          `mem0-mcp-server` (add_memory, search_memories, get_memories, get_memory,
          update_memory, delete_memory), but backed by the open-source `Memory` library.

Why the second mode exists: the project's own MCP server and Claude Code plugin talk to the hosted
Mem0 Platform (an account and an API key), and the open-source OpenMemory MCP server is no longer
shipped. A Claude Code user who wants the open-source library has no documented MCP route, so this
bridge is the harness's, not the project's, and SYSTEMS.md says so.

Configuration comes from the environment: MEM0_DIR (state root), LONGHAUL_LLM_BASE_URL /
LONGHAUL_LLM_MODEL / LONGHAUL_LLM_API_KEY, LONGHAUL_EMBED_MODEL. Standard library plus mem0.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

os.environ.setdefault("MEM0_TELEMETRY", "false")  # the documented opt-out
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")


def build_memory():
    from mem0 import Memory

    root = Path(os.environ["MEM0_DIR"])
    base = os.environ.get("LONGHAUL_LLM_BASE_URL", "")
    llm_cfg = {
        "model": os.environ.get("LONGHAUL_LLM_MODEL") or "fake-model",
        "api_key": os.environ.get("LONGHAUL_LLM_API_KEY") or "sk-not-a-key",
    }
    if base:
        llm_cfg["openai_base_url"] = base
    embed_model = os.environ.get("LONGHAUL_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
    config = {
        "llm": {"provider": "openai", "config": llm_cfg},
        "embedder": {"provider": "fastembed", "config": {"model": embed_model}},
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "path": str(root / "qdrant"),
                "collection_name": "longhaul",
                "embedding_model_dims": int(os.environ.get("LONGHAUL_EMBED_DIMS", "384")),
                "on_disk": True,
            },
        },
        "history_db_path": str(root / "history.db"),
    }
    return Memory.from_config(config)


def op(memory, req: dict):
    kind = req["op"]
    if kind == "add":
        return memory.add(
            req["messages"],
            user_id=req.get("user_id"),
            agent_id=req.get("agent_id"),
            run_id=req.get("run_id"),
            metadata=req.get("metadata"),
            infer=req.get("infer", True),
        )
    if kind == "search":
        return memory.search(req["query"], top_k=req.get("top_k", 10), filters=req["filters"])
    if kind == "get_all":
        return memory.get_all(filters=req["filters"], top_k=req.get("top_k", 1000))
    if kind == "get":
        return memory.get(req["id"])
    if kind == "update":
        return memory.update(req["id"], req["data"])
    if kind == "delete":
        return memory.delete(req["id"])
    raise ValueError(f"unknown op {kind}")


def rpc() -> None:
    memory = build_memory()
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            print(
                json.dumps({"ok": True, "result": op(memory, json.loads(line))}, default=str),
                flush=True,
            )
        except Exception as e:  # keep serving; the adapter raises on ok=false
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": f"{type(e).__name__}: {e}",
                        "trace": traceback.format_exc()[-800:],
                    }
                ),
                flush=True,
            )


# ---- MCP mode ---------------------------------------------------------------------------------

DEFAULT_USER = os.environ.get("MEM0_DEFAULT_USER_ID", "mem0-mcp")  # the project's own variable name

TOOLS = [
    {
        "name": "add_memory",
        "description": "Save text or conversation history for a user/agent. Mem0 extracts the facts worth keeping.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to store."},
                "user_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "run_id": {"type": "string"},
                "metadata": {"type": "object"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "search_memories",
        "description": "Semantic search across existing memories.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "filters": {"type": "object", "description": 'e.g. {"user_id": "..."}'},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_memories",
        "description": "List memories with structured filters.",
        "inputSchema": {
            "type": "object",
            "properties": {"filters": {"type": "object"}, "limit": {"type": "integer"}},
        },
    },
    {
        "name": "get_memory",
        "description": "Retrieve one memory by its memory_id.",
        "inputSchema": {
            "type": "object",
            "properties": {"memory_id": {"type": "string"}},
            "required": ["memory_id"],
        },
    },
    {
        "name": "update_memory",
        "description": "Overwrite a memory's text once the user confirms the memory_id.",
        "inputSchema": {
            "type": "object",
            "properties": {"memory_id": {"type": "string"}, "text": {"type": "string"}},
            "required": ["memory_id", "text"],
        },
    },
    {
        "name": "delete_memory",
        "description": "Delete a single memory by memory_id.",
        "inputSchema": {
            "type": "object",
            "properties": {"memory_id": {"type": "string"}},
            "required": ["memory_id"],
        },
    },
]


def call_tool(memory, name: str, a: dict):
    uid = a.get("user_id") or DEFAULT_USER
    if name == "add_memory":
        return memory.add(
            [{"role": "user", "content": a["text"]}],
            user_id=uid,
            agent_id=a.get("agent_id"),
            run_id=a.get("run_id"),
            metadata=a.get("metadata"),
        )
    if name == "search_memories":
        return memory.search(
            a["query"], top_k=a.get("limit", 10), filters=a.get("filters") or {"user_id": uid}
        )
    if name == "get_memories":
        return memory.get_all(
            filters=a.get("filters") or {"user_id": uid}, top_k=a.get("limit", 100)
        )
    if name == "get_memory":
        return memory.get(a["memory_id"])
    if name == "update_memory":
        return memory.update(a["memory_id"], a["text"])
    if name == "delete_memory":
        return memory.delete(a["memory_id"])
    raise ValueError(f"unknown tool {name}")


def mcp() -> None:
    memory = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:  # notification
            continue
        try:
            if method == "initialize":
                res = {
                    "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mem0-oss-bridge", "version": "1"},
                }
            elif method == "tools/list":
                res = {"tools": TOOLS}
            elif method == "tools/call":
                if memory is None:
                    memory = build_memory()
                p = msg["params"]
                try:
                    out = call_tool(memory, p["name"], p.get("arguments") or {})
                    res = {"content": [{"type": "text", "text": json.dumps(out, default=str)}]}
                except Exception as e:
                    res = {
                        "isError": True,
                        "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                    }
            elif method == "ping":
                res = {}
            else:
                print(
                    json.dumps(
                        {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": method}}
                    ),
                    flush=True,
                )
                continue
            print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}), flush=True)
        except Exception as e:
            print(
                json.dumps(
                    {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}
                ),
                flush=True,
            )


if __name__ == "__main__":
    {"--rpc": rpc, "--mcp": mcp}[sys.argv[1]]()
