#!/usr/bin/env python3
"""A tiny fake OpenAI-compatible server for proving that a memory product's model plumbing works
without any real model, key or account.

It serves `/v1/chat/completions` (plain, streamed, JSON-schema and tool-call forms),
`/v1/responses`, `/v1/embeddings` and `/v1/models`, answers every request with deterministic
canned output, and counts what it was asked. Counts are read from `GET /_stats` (totals and per
kind) and reset with `POST /_reset`; every request is also appended to `--log` as one JSON line.
Token counts are an estimate (characters / 4), the same for every product.

The canned output is only as smart as it needs to be to keep a product's pipeline running:

* mem0's memory extractor gets `{"memory": [...]}` with one entry per message line of the
  conversation it was shown (the whole conversation, verbatim, as "extracted facts");
* any request carrying a JSON schema (`response_format`, `text.format`, a forced tool) gets a
  minimal instance of that schema, with strings filled from the conversation where the field
  name suggests it (names, summaries, facts);
* claude-mem's observer gets one `<observation>` block quoting the tool activity it was shown,
  and a `<summary>` when asked for one;
* anything else gets a short fixed sentence.

Usage: fake_openai.py --port 0 --port-file PATH [--log PATH] [--embedding-dims 384]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FAKE_ENTITIES = int(
    __import__("os").environ.get("FAKE_ENTITIES", "3")
)  # entities per episode for graph extractors
STATE = {
    "lock": threading.Lock(),
    "calls": [],
    "dims": 384,
    "log": None,
    "latency": 0.0,
    "dump": None,
}


def est_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


def flatten(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(flatten(p.get("text", p) if isinstance(p, dict) else p) for p in content)
    if isinstance(content, dict):
        return str(content.get("text", ""))
    return "" if content is None else str(content)


def messages_text(body: dict) -> tuple[str, str]:
    """(system text, last user message) of a chat or responses request."""
    system, rest = [], []
    msgs = body.get("messages")
    if msgs is None:
        inp = body.get("input", "")
        msgs = [{"role": "user", "content": inp}] if isinstance(inp, str) else inp
        if body.get("instructions"):
            system.append(str(body["instructions"]))
    for m in msgs or []:
        if not isinstance(m, dict):
            continue
        if m.get("role") in ("system", "developer"):
            system.append(flatten(m.get("content")))
        elif m.get("role") == "user":
            rest = [
                flatten(m.get("content"))
            ]  # the request being answered is the last user message
    return "\n".join(system), "\n".join(rest)


# ---- schema-driven minimal instances -------------------------------------------------------


def deref(schema, root):
    while isinstance(schema, dict) and "$ref" in schema:
        node = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node.get(part, {})
        schema = node
    return schema


def instance(schema, root, hint: str, name: str = "", depth: int = 0):
    schema = deref(schema, root)
    if not isinstance(schema, dict) or depth > 6:
        return None
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [o for o in schema[key] if deref(o, root).get("type") != "null"]
            return instance(options[0], root, hint, name, depth + 1) if options else None
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "null")
    if t == "object" or "properties" in schema:
        out = {}
        for k, sub in (schema.get("properties") or {}).items():
            out[k] = instance(sub, root, hint, k, depth + 1)
        return out
    if t == "array":
        if name in (
            "entities",
            "extracted_entities",
            "nodes",
            "edges",
            "facts",
            "relations",
            "memory",
        ):
            return []
        return []
    if t == "integer":
        return 0
    if t == "number":
        return 0.0
    if t == "boolean":
        return False
    if t == "string":
        low = name.lower()
        if low in ("summary", "description", "fact", "text", "content", "name", "title"):
            return " ".join(hint.split())[:200] or "canned"
        return "canned"
    return None


def first_schema(body: dict):
    rf = body.get("response_format") or {}
    if rf.get("type") == "json_schema":
        js = rf.get("json_schema") or {}
        return js.get("schema") or {}
    fmt = (body.get("text") or {}).get("format") or {}
    if fmt.get("type") == "json_schema":
        return fmt.get("schema") or {}
    tools = body.get("tools") or []
    choice = body.get("tool_choice")
    if tools and choice not in (None, "none", "auto"):
        fn = tools[0].get("function") or tools[0]
        return fn.get("parameters") or fn.get("input_schema") or {}
    return None


# ---- product-shaped canned answers -----------------------------------------------------------


def last_hint(user: str) -> str:
    lines = [ln.strip() for ln in user.splitlines() if ln.strip()]
    return lines[-1][:300] if lines else ""


def classify(system: str, user: str, body: dict) -> str:
    s = system.lower()
    u = user.lower()
    if "memory extractor" in s or "personal information organizer" in s:
        return "mem0.extract"
    if "smart memory manager" in s or "memory manager" in s and "event" in s:
        return "mem0.update"
    if "progress summary" in u or "<summary>" in u:
        return "claude-mem.summary"
    if "<observation" in s or "observer" in s and "claude-mem" in s or "memory agent" in s:
        return "claude-mem.observe"
    if first_schema(body) is not None:
        return "schema"
    return "other"


def canned(kind: str, system: str, user: str, body: dict) -> tuple[str, list | None]:
    if kind == "mem0.extract":
        block = re.search(r"## New Messages\s*(.*?)\s*## Observation Date", user, re.S)
        lines = [
            ln.strip() for ln in (block.group(1) if block else user).splitlines() if ln.strip()
        ]
        mem = []
        for ln in lines[:6]:
            m = re.match(r"^(user|assistant)\s*:\s*(.+)$", ln, re.I)
            if m and len(m.group(2)) > 8:
                mem.append(
                    {
                        "id": str(len(mem)),
                        "text": m.group(2)[:400],
                        "attributed_to": m.group(1).lower(),
                    }
                )
        return json.dumps({"memory": mem}), None
    if kind in ("claude-mem.observe", "claude-mem.summary"):
        m = re.findall(r"<outcome>(.*?)</outcome>|<user_request>(.*?)</user_request>", user, re.S)
        found = [a or b for a, b in m]
        quote = " ".join(" ".join(found[-3:] if found else [user]).split())[-400:] or "activity"
        quote = quote.replace("<", "(").replace(">", ")").replace("&", "and")
        if kind == "claude-mem.summary":
            resp = re.search(r"Full Response to User:\s*(.*?)\s*Respond in this XML", user, re.S)
            if resp:
                quote = " ".join(resp.group(1).split())[:400].replace("<", "(").replace(">", ")")
            return (
                f"<summary><request>canned</request><investigated>{quote[:80]}</investigated>"
                f"<learned>{quote[:120]}</learned><completed>canned</completed>"
                f"<next_steps>none</next_steps><notes>canned</notes></summary>"
            ), None
        return (
            f"<observation><type>discovery</type><title>{quote[:60]}</title>"
            f"<subtitle>canned</subtitle><facts><fact>{quote[:200]}</fact></facts>"
            f"<narrative>{quote}</narrative><concepts><concept>how-it-works</concept></concepts>"
            f"<files_read></files_read><files_modified></files_modified></observation>"
        ), None
    schema = first_schema(body)
    if schema is not None:
        hint = last_hint(user)
        value = instance(schema, schema, hint)
        value = graphiti_override(schema.get("title", ""), value, user)
        if body.get("tools") and body.get("tool_choice") not in (None, "none", "auto"):
            fn = (body["tools"][0].get("function") or body["tools"][0]).get("name", "fn")
            return "", [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": fn, "arguments": json.dumps(value)},
                }
            ]
        return json.dumps(value), None
    return "Acknowledged.", None


def graphiti_override(title: str, value, user: str):
    """Graphiti's extraction steps get a few entities and facts quoted from the episode, so its
    deduplication and edge steps run as they would on real output (a real model typically
    extracts more, so the counts measured here are a floor)."""
    cur = re.search(r"<CURRENT[ _]MESSAGE>\s*(.*?)\s*</CURRENT[ _]MESSAGE>", user, re.S)
    text = cur.group(1) if cur else ""
    if title == "ExtractedEntities" and isinstance(value, dict):
        names: list[str] = []
        for ln in text.splitlines():
            m = re.match(r"^\s*([A-Za-z][\w .-]{0,30}?)\s*:", ln)
            if m and m.group(1) not in names:
                names.append(m.group(1))
        words = sorted(
            set(re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{4,}", text)), key=lambda w: (-len(w), w)
        )
        names = (names[:2] + words)[:FAKE_ENTITIES]
        value["extracted_entities"] = [
            {"name": n, "entity_type_id": 0, "episode_indices": [0]} for n in names
        ]
    elif title == "ExtractedEdges" and isinstance(value, dict):
        ents = re.search(r"<ENTITIES>\s*(\[.*?\])\s*</ENTITIES>", user, re.S)
        try:
            names = [e["name"] for e in json.loads(ents.group(1))] if ents else []
        except (ValueError, KeyError, TypeError):
            names = []
        fact = " ".join(text.split())[:300]
        value["edges"] = [
            {
                "source_entity_name": a,
                "target_entity_name": b,
                "relation_type": "MENTIONS",
                "fact": fact,
                "valid_at": None,
                "invalid_at": None,
                "episode_indices": [0],
            }
            for a, b in zip(names, names[1:], strict=False)
        ][:FAKE_ENTITIES]
    return value


def embed(text: str, dims: int) -> list[float]:
    """A deterministic bag-of-words hash embedding (unit length), so similar texts are close."""
    vec = [0.0] * dims
    for w in re.findall(r"\w+", text.lower()) or [""]:
        h = int(hashlib.sha1(w.encode()).hexdigest(), 16)
        vec[h % dims] += 1.0 if (h >> 8) & 1 else -1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def record(entry: dict) -> None:
    with STATE["lock"]:
        STATE["calls"].append(entry)
        if STATE["log"]:
            with open(STATE["log"], "a") as f:
                f.write(json.dumps(entry) + "\n")


def stats() -> dict:
    with STATE["lock"]:
        calls = list(STATE["calls"])
    by_kind: dict[str, dict] = {}
    for c in calls:
        k = by_kind.setdefault(c["kind"], {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
        k["calls"] += 1
        k["prompt_tokens"] += c["prompt_tokens"]
        k["completion_tokens"] += c["completion_tokens"]
    chat = [c for c in calls if c["endpoint"] != "embeddings"]
    emb = [c for c in calls if c["endpoint"] == "embeddings"]
    return {
        "chat_calls": len(chat),
        "chat_prompt_tokens": sum(c["prompt_tokens"] for c in chat),
        "chat_completion_tokens": sum(c["completion_tokens"] for c in chat),
        "embedding_calls": len(emb),
        "embedding_tokens": sum(c["prompt_tokens"] for c in emb),
        "by_kind": by_kind,
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def _json(self, code: int, obj) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/_stats":
            return self._json(200, stats())
        if path.endswith("/models"):
            return self._json(
                200,
                {
                    "object": "list",
                    "data": [{"id": "fake-model", "object": "model", "owned_by": "fake"}],
                },
            )
        if path in ("/", "/health", "/healthz"):
            return self._json(200, {"ok": True})
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        path = self.path.split("?")[0]
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._json(400, {"error": {"message": "bad json"}})
        if path == "/_reset":
            with STATE["lock"]:
                STATE["calls"].clear()
            return self._json(200, {"ok": True})
        if STATE["latency"]:
            time.sleep(STATE["latency"])
        if path.endswith("/embeddings"):
            inp = body.get("input", "")
            items = [inp] if isinstance(inp, str) else list(inp)
            dims = int(body.get("dimensions") or STATE["dims"])
            toks = sum(est_tokens(str(x)) for x in items)
            record(
                {
                    "t": time.time(),
                    "endpoint": "embeddings",
                    "kind": "embedding",
                    "prompt_tokens": toks,
                    "completion_tokens": 0,
                    "items": len(items),
                }
            )
            return self._json(
                200,
                {
                    "object": "list",
                    "model": body.get("model", "fake-embed"),
                    "data": [
                        {"object": "embedding", "index": i, "embedding": embed(str(x), dims)}
                        for i, x in enumerate(items)
                    ],
                    "usage": {"prompt_tokens": toks, "total_tokens": toks},
                },
            )
        if path.endswith("/chat/completions") or path.endswith("/responses"):
            system, user = messages_text(body)
            kind = classify(system, user, body)
            if kind == "schema":
                kind = "schema:" + str((first_schema(body) or {}).get("title", ""))
            text, tool_calls = canned(kind, system, user, body)
            if STATE["dump"]:
                with STATE["lock"]:
                    idx = len(STATE["calls"])
                with open(f"{STATE['dump']}/{idx:05d}-{kind}.json", "w") as f:
                    json.dump(body, f, indent=1)
            pt = est_tokens(system + user + json.dumps(body.get("tools") or ""))
            ct = est_tokens(text or json.dumps(tool_calls))
            record(
                {
                    "t": time.time(),
                    "endpoint": "responses" if path.endswith("/responses") else "chat",
                    "kind": kind,
                    "model": body.get("model"),
                    "prompt_tokens": pt,
                    "completion_tokens": ct,
                    "stream": bool(body.get("stream")),
                }
            )
            if path.endswith("/responses"):
                return self._json(200, self._responses_obj(body, text, pt, ct))
            if body.get("stream"):
                return self._stream(body, text, tool_calls, pt, ct)
            msg = {"role": "assistant", "content": text if not tool_calls else None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            return self._json(
                200,
                {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": body.get("model", "fake-model"),
                    "choices": [
                        {
                            "index": 0,
                            "message": msg,
                            "finish_reason": "tool_calls" if tool_calls else "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": pt,
                        "completion_tokens": ct,
                        "total_tokens": pt + ct,
                    },
                },
            )
        self._json(404, {"error": {"message": f"no route {path}"}})

    @staticmethod
    def _responses_obj(body, text, pt, ct):
        return {
            "id": "resp_fake",
            "object": "response",
            "created_at": int(time.time()),
            "status": "completed",
            "model": body.get("model", "fake-model"),
            "output": [
                {
                    "type": "message",
                    "id": "msg_fake",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                }
            ],
            "output_text": text,
            "usage": {"input_tokens": pt, "output_tokens": ct, "total_tokens": pt + ct},
        }

    def _stream(self, body, text, tool_calls, pt, ct):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(delta, finish=None, usage=None):
            obj = {
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": body.get("model", "fake-model"),
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            if usage:
                obj["usage"] = usage
            payload = f"data: {json.dumps(obj)}\n\n".encode()
            self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")

        chunk({"role": "assistant", "content": ""})
        if tool_calls:
            tc = dict(tool_calls[0], index=0)
            chunk({"tool_calls": [tc]})
        else:
            for i in range(0, len(text), 400):
                chunk({"content": text[i : i + 400]})
        chunk({}, "tool_calls" if tool_calls else "stop")
        usage = {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}
        if (body.get("stream_options") or {}).get("include_usage"):
            obj = {
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": body.get("model", "fake-model"),
                "choices": [],
                "usage": usage,
            }
            payload = f"data: {json.dumps(obj)}\n\n".encode()
            self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
        done = b"data: [DONE]\n\n"
        self.wfile.write(f"{len(done):x}\r\n".encode() + done + b"\r\n0\r\n\r\n")
        self.wfile.flush()


def serve(port: int = 0, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--port-file", help="write the bound port here once listening")
    ap.add_argument("--log", help="append one JSON line per request")
    ap.add_argument("--embedding-dims", type=int, default=384)
    ap.add_argument(
        "--entities",
        type=int,
        default=FAKE_ENTITIES,
        help="entities per episode for graph extractors",
    )
    ap.add_argument("--dump", help="directory to write each chat request body into (debugging)")
    ap.add_argument("--latency", type=float, default=0.0, help="seconds to sleep per request")
    a = ap.parse_args()
    globals()["FAKE_ENTITIES"] = a.entities
    STATE["dims"], STATE["log"], STATE["latency"], STATE["dump"] = (
        a.embedding_dims,
        a.log,
        a.latency,
        a.dump,
    )
    srv = serve(a.port)
    if a.port_file:
        with open(a.port_file, "w") as f:
            f.write(str(srv.server_address[1]))
    print(f"fake openai server on 127.0.0.1:{srv.server_address[1]}", file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
