"""Disposable CLI checkpoint/resume and real local-file operation readback walkthrough."""

import hashlib
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
    body = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures/contracts/checkpoint.json").read_text()
    )
    with tempfile.TemporaryDirectory(prefix="mk-working-", dir="/tmp") as directory:
        root = Path(directory)
        credential_file = LocalStore.initialize(root / "db", root / "ctl")
        credential = credential_file.read_text()
        with LocalStore(root / "db", root / "ctl") as store, Server(store) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            sequence = 0
            scope = {"project": "demo", "task": "task", "run": None}

            def invoke(action, args):
                nonlocal sequence
                sequence += 1
                command = {
                    "version": 1,
                    "incarnation": store.incarnation,
                    "action": action,
                    "args": args,
                }
                if action not in {"resume", "operations"}:
                    command["key"] = f"demo{sequence}"
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
                    input=canonical_bytes({"operation": "working", "args": {"command": command}}),
                    capture_output=True,
                    env=env,
                    check=True,
                )
                raw = run.stdout.removesuffix(b"\n")
                result = json.loads(raw)
                assert canonical_bytes(result) == raw
                return result

            try:
                saved = invoke(
                    "save",
                    {**scope, "expected_revision": 0, "readers": ["owner"], "checkpoint": body},
                )
                resume = {
                    **scope,
                    "checkpoint": saved["checkpoint"],
                    "bindings": [{}],
                    "max_output_tokens": 6000,
                    "tokenizer": "cl100k_base",
                }
                packet = invoke("resume", resume)
                assert packet["status"] == "ready"
                assert packet["checkpoint"]["state"] == body
                path = root / "external-result.txt"
                content = b"A disposable, authorized local effect.\n"
                invoke(
                    "operation_intent",
                    {
                        **scope,
                        "operation": "file-write",
                        "target_hash": hashlib.sha256(str(path).encode()).hexdigest(),
                        "parameters_hash": hashlib.sha256(content).hexdigest(),
                        "method": "readback",
                        "provider_key": None,
                    },
                )
                transition = {
                    "project": "demo",
                    "operation": "file-write",
                    "expected_revision": 1,
                    "state": "dispatching",
                    "reconciled": False,
                    "evidence": [],
                    "result_hash": None,
                }
                invoke("operation_transition", transition)
                with path.open("xb") as target:
                    target.write(content)
                    target.flush()
                    os.fsync(target.fileno())
                # Explicitly record an unconfirmed result; only readback may resolve it.
                invoke(
                    "operation_transition",
                    {**transition, "expected_revision": 2, "state": "unknown"},
                )
                assert invoke("resume", resume)["status"] == "incomplete"
                assert path.read_bytes() == content
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                observation = store.capture(
                    credential,
                    store.namespace(credential, "demo"),
                    "readback",
                    {
                        "version": 1,
                        "project": "demo",
                        "source": "file-readback",
                        "source_event": "file-write",
                        "text": "Readback confirmed the disposable file content SHA256 " + digest,
                        "readers": ["owner"],
                        "classification": "internal",
                        "observed_at_ms": store.clock.utc_ms(),
                    },
                )
                invoke(
                    "operation_transition",
                    {
                        **transition,
                        "expected_revision": 3,
                        "state": "succeeded",
                        "reconciled": True,
                        "evidence": [{"kind": "observation", "id": observation["observation"]}],
                        "result_hash": digest,
                    },
                )
                assert invoke("resume", resume)["status"] == "ready"
                assert path.read_bytes() == content
                print(
                    json.dumps(
                        {
                            "checks": [
                                "complete_checkpoint_cli",
                                "full_essential_resume",
                                "real_local_file_readback",
                                "ambiguous_operation_reconciled_without_repeat",
                            ],
                            "external_calls": 0,
                            "harness_qualification": "pending",
                        }
                    )
                )
            finally:
                server.shutdown()
                worker.join()


if __name__ == "__main__":
    main()
