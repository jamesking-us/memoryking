"""Run a readable, model-free capture/brief/checkpoint/resume example."""

import json
import tempfile
import threading
import time
from pathlib import Path

from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
from memoryking.sdk import LocalClient


def main():
    with tempfile.TemporaryDirectory(prefix="mk-example-", dir="/tmp") as directory:
        root = Path(directory)
        credential = LocalStore.initialize(root / "data", root / "control")
        with LocalStore(root / "data", root / "control") as store, Server(store) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                first = LocalClient(server.path, credential, "checkout")
                finding = "In the demo, increasing every timeout exceeded the total retry deadline."
                first.capture(
                    "retry-finding-1",
                    {
                        "version": 1,
                        "project": "checkout",
                        "source": "example-agent",
                        "source_event": "retry-test-1",
                        "text": finding,
                        "readers": ["owner"],
                        "classification": "internal",
                        "observed_at_ms": time.time_ns() // 1_000_000,
                    },
                )
                print("1. Agent A saved a finding: " + finding)

                # This bundled synthetic checkpoint makes every required field visible.
                checkpoint = json.loads(
                    (
                        Path(__file__).resolve().parents[1] / "fixtures/contracts/checkpoint.json"
                    ).read_text()
                )
                checkpoint["objective"] = "Fix retry behavior without extending the total deadline"
                checkpoint["current_state"] = "Investigation saved; no fix has been verified"
                scope = {"project": "checkout", "task": "retry-fix", "run": None}
                saved = first.working(
                    {
                        "version": 1,
                        "incarnation": first.binding[-1],
                        "action": "save",
                        "key": "checkpoint-1",
                        "args": {
                            **scope,
                            "expected_revision": 0,
                            "readers": ["owner"],
                            "checkpoint": checkpoint,
                        },
                    }
                )
                print("2. Agent A saved the unfinished task, failed attempt and next actions.")
                del first

                # A new connection represents the next session of the same authorized agent.
                second = LocalClient(server.path, credential, "checkout")
                brief = second.retrieval(
                    {
                        "version": 1,
                        "incarnation": second.binding[-1],
                        "action": "brief",
                        "args": {
                            "project": "checkout",
                            "query": "timeout",
                            "bindings": [{}],
                            "max_output_tokens": 6000,
                            "tokenizer": "utf8-byte-estimate-v1",
                        },
                    }
                )
                assert brief["guidance"] and brief["guidance"][0]["authority"] == "observed_episode"
                assert brief["usage"]["counting"] == "estimate"
                print(
                    "3. A new session recalled the finding as an observation, not an accepted rule."
                )
                resumed = second.working(
                    {
                        "version": 1,
                        "incarnation": second.binding[-1],
                        "action": "resume",
                        "args": {
                            **scope,
                            "checkpoint": saved["checkpoint"],
                            "bindings": [{}],
                            "max_output_tokens": 12000,
                            "tokenizer": "utf8-byte-estimate-v1",
                        },
                    }
                )
                assert resumed["status"] == "ready", resumed["status"]
                state = resumed["checkpoint"]["state"]
                assert state == checkpoint
                print("4. Resume goal: " + state["objective"])
                print("   Do not repeat: " + state["failed_attempts"][0]["action"])
                print("   Next action: " + state["next_actions"][0])
                print("   Verification: still pending; no model or external tool was called.")
                print("Walkthrough passed. Temporary example data will now be removed.")
            finally:
                server.shutdown()
                thread.join(timeout=5)


if __name__ == "__main__":
    main()
