"""Disposable real CLI/socket briefing, fetch, receipt, replay and rerun walkthrough."""

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


def main():
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"PYTHONPATH", "PYTHONHOME", "GMAIL_FROM", "GMAIL_APP_KEY", "KINDLE_TO"}
    }
    with tempfile.TemporaryDirectory(prefix="mk-brief-", dir="/tmp") as directory:
        root = Path(directory)
        credential_file = LocalStore.initialize(root / "db", root / "ctl")
        credential = credential_file.read_text()
        with LocalStore(root / "db", root / "ctl") as store:
            namespace = store.namespace(credential, "demo")
            captured = store.capture(
                credential,
                namespace,
                "demo",
                {
                    "version": 1,
                    "project": "demo",
                    "source": "demo",
                    "source_event": "1",
                    "text": "Observed timeout during retry.",
                    "readers": None,
                    "classification": "internal",
                    "observed_at_ms": 1,
                },
            )
            with Server(store) as server:
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()

                def invoke(action, args):
                    command = {
                        "operation": "retrieval",
                        "args": {
                            "command": {
                                "version": 1,
                                "incarnation": store.incarnation,
                                "action": action,
                                "args": {"project": "demo", **args},
                            }
                        },
                    }
                    run = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "memoryking",
                            "request",
                            "--socket",
                            str(server.path),
                            "--credential",
                            str(credential_file),
                        ],
                        input=canonical_bytes(command),
                        capture_output=True,
                        env=env,
                        check=True,
                    )
                    return run.stdout.removesuffix(b"\n")

                try:
                    brief_args = {
                        "query": "timeout",
                        "bindings": [{}],
                        "max_output_tokens": 3000,
                        "tokenizer": "cl100k_base",
                    }
                    raw = invoke("brief", brief_args)
                    result = json.loads(raw)
                    assert raw == canonical_bytes(result)
                    assert result["guidance"][0]["authority"] == "observed_episode"
                    receipt = {"receipt": result["receipt"]}
                    assert invoke("replay", receipt) == raw
                    diag = json.loads(invoke("receipt", receipt))["diagnostics"]
                    assert diag["usage"]["output_tokens"] <= 3000
                    fetched = json.loads(
                        invoke(
                            "fetch",
                            {
                                "reference": captured["observation"],
                                "kind": "observation",
                                "start_byte": 0,
                                "end_byte": 8,
                                "max_output_tokens": 2000,
                                "tokenizer": "cl100k_base",
                            },
                        )
                    )
                    assert fetched["excerpt"] == "Observed"
                    rerun = json.loads(
                        invoke(
                            "rerun",
                            {
                                "predecessor": result["receipt"],
                                "brief": {"project": "demo", **brief_args},
                            },
                        )
                    )
                    assert rerun["receipt"] != result["receipt"]
                    assert json.loads(invoke("receipts", {}))["items"]
                    print(
                        json.dumps(
                            {
                                "checks": [
                                    "canonical_cli_budget",
                                    "attributed_lexical_brief",
                                    "bounded_evidence_fetch",
                                    "inspectable_receipt",
                                    "exact_replay_bytes",
                                    "explicit_rerun_new_receipt",
                                ],
                                "passed": True,
                            }
                        )
                    )
                finally:
                    server.shutdown()
                    worker.join(timeout=5)


if __name__ == "__main__":
    main()
