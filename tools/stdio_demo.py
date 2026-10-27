"""Exercise the local Python SDK and MCP stdio subprocess with a disposable daemon."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
from memoryking.contracts.canonical import canonical_bytes
from memoryking.sdk import LocalClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tokenizer", default="cl100k_base", choices=["cl100k_base", "utf8-byte-estimate-v1"]
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mk-stdio-", dir="/tmp") as directory:
        root = Path(directory)
        credential = LocalStore.initialize(root / "db", root / "ctl")
        with LocalStore(root / "db", root / "ctl") as store, Server(store) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                client = LocalClient(server.path, credential, "demo")
                context = client.context()
                assert context["principal"] == "owner"
                client.capture(
                    "demo",
                    {
                        "version": 1,
                        "project": "demo",
                        "source": "sdk",
                        "source_event": "1",
                        "text": "Observed a galaxy in the disposable demo.",
                        "readers": ["owner"],
                        "classification": "internal",
                        "observed_at_ms": 1,
                    },
                )
                brief = {
                    "version": 1,
                    "incarnation": context["incarnation"],
                    "action": "brief",
                    "args": {
                        "project": "demo",
                        "query": "galaxy",
                        "bindings": [{}],
                        "max_output_tokens": 3000,
                        "tokenizer": args.tokenizer,
                    },
                }
                messages = [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {},
                            "clientInfo": {"name": "memoryking-demo", "version": "1"},
                        },
                    },
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "memory_brief", "arguments": brief},
                    },
                ]
                plan_body = json.loads(
                    (Path(__file__).resolve().parents[1] / "examples/plan.json").read_text()
                )
                for ident, action, arguments in [
                    (
                        4,
                        "plan_save",
                        {
                            "task": "stdio-plan",
                            "expected_revision": 0,
                            "readers": ["owner"],
                            "plan": plan_body,
                        },
                    ),
                    (5, "plan_get", {"task": "stdio-plan", "revision": None}),
                ]:
                    command = {
                        "version": 1,
                        "incarnation": context["incarnation"],
                        "action": action,
                        "args": {"project": "demo", **arguments},
                    }
                    if action == "plan_save":
                        command["key"] = "stdio-plan"
                    messages.append(
                        {
                            "jsonrpc": "2.0",
                            "id": ident,
                            "method": "tools/call",
                            "params": {"name": "memory_plan", "arguments": command},
                        }
                    )
                run = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "memoryking.adapters.stdio",
                        "--socket",
                        str(server.path),
                        "--credential",
                        str(credential),
                        "--project",
                        "demo",
                    ],
                    input=b"".join(canonical_bytes(m) + b"\n" for m in messages),
                    capture_output=True,
                    timeout=20,
                    check=True,
                    env={
                        k: v
                        for k, v in os.environ.items()
                        if k
                        not in {
                            "PYTHONHOME",
                            "PYTHONPATH",
                            "GMAIL_FROM",
                            "GMAIL_APP_KEY",
                            "KINDLE_TO",
                        }
                    },
                )
                responses = [json.loads(line) for line in run.stdout.splitlines()]
                assert len(responses) == 5 and not run.stderr
                assert len(responses[1]["result"]["tools"]) == 9
                assert not responses[-1]["result"]["isError"]
                assert (
                    json.loads(responses[-1]["result"]["content"][0]["text"])["progress"]["total"]
                    == 2
                )
                raw = responses[2]["result"]["content"][0]["text"].encode()
                result = json.loads(raw)
                assert canonical_bytes(result) == raw and result["guidance"]
                assert result["usage"]["counting"] == (
                    "exact" if args.tokenizer == "cl100k_base" else "estimate"
                )
                print(
                    json.dumps(
                        {
                            "checks": [
                                "bound_python_sdk",
                                "real_mcp_stdio_subprocess",
                                "nine_implemented_tools",
                                "canonical_counted_text",
                            ],
                            "provider_calls": 0,
                            "counting": result["usage"]["counting"],
                            "third_party_host_qualification": "not_evaluated",
                        }
                    )
                )
            finally:
                server.shutdown()
                worker.join()


if __name__ == "__main__":
    main()
