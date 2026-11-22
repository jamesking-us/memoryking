#!/usr/bin/env python3
"""A minimal OpenAI-compatible `/v1/embeddings` server over a local sentence-transformers model.

Graphiti's server can only reach an embedding model through an OpenAI-compatible endpoint (its
embedder providers are OpenAI, Azure, Gemini and Voyage, and the docs point local setups at an
OpenAI-compatible server such as Ollama). This is that role, filled with a model that runs on this
machine and costs nothing; it makes no network call except the one-time model download from
huggingface.co. Run it with a Python that has `sentence-transformers` (the Graphiti virtualenv).

Usage: local_embeddings.py --port-file PATH [--model all-MiniLM-L6-v2] [--port 0]
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="all-MiniLM-L6-v2")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--port-file")
    a = ap.parse_args()
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(a.model, device="cpu")

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._send(200, {"ok": True, "model": a.model})

        def do_POST(self):
            body = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}"
            )
            inp = body.get("input", "")
            items = [inp] if isinstance(inp, str) else list(inp)
            vecs = model.encode([str(x) for x in items], normalize_embeddings=True).tolist()
            toks = sum(max(1, len(str(x)) // 4) for x in items)
            print(
                f"embeddings request: {len(items)} items, {toks} tokens (estimate)",
                file=sys.stderr,
                flush=True,
            )
            self._send(
                200,
                {
                    "object": "list",
                    "model": body.get("model", a.model),
                    "data": [
                        {"object": "embedding", "index": i, "embedding": v}
                        for i, v in enumerate(vecs)
                    ],
                    "usage": {"prompt_tokens": toks, "total_tokens": toks},
                },
            )

    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    if a.port_file:
        with open(a.port_file, "w") as f:
            f.write(str(srv.server_address[1]))
    print(f"local embeddings on 127.0.0.1:{srv.server_address[1]}", file=sys.stderr, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
