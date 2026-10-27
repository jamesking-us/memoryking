"""Create a plan, record real local evidence, update a step and read its progress."""

import json
import tempfile
import threading
from pathlib import Path

from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
from memoryking.sdk import LocalClient

ROOT = Path(__file__).resolve().parents[1]


def exercise(client):
    body = json.loads((ROOT / "examples/plan.json").read_text())
    incarnation = client.context()["incarnation"]

    def command(action, args, key=None):
        value = {
            "version": 1,
            "incarnation": incarnation,
            "action": action,
            "args": {"project": client.project, **args},
        }
        if key is not None:
            value["key"] = key
        # Same closed tool contract used by stdio and the shared MCP endpoint.
        return client.tool("memory_plan", value)

    command(
        "plan_save",
        {"task": "checkout-plan", "expected_revision": 0, "readers": ["owner"], "plan": body},
        "create-plan",
    )
    proof = client.capture(
        "inspection-proof",
        {
            "version": 1,
            "project": client.project,
            "source": "plans-demo",
            "source_event": "inspection",
            "text": "Inspected the synthetic plan: two phases and two ordered steps.",
            "readers": ["owner"],
            "classification": "internal",
            "observed_at_ms": 1,
        },
    )
    command(
        "plan_step",
        {
            "task": "checkout-plan",
            "expected_revision": 1,
            "step": "inspect",
            "status": "done",
            "notes": "Inspected the example definition.",
            "decision": None,
            "evidence": [{"kind": "observation", "id": proof["observation"]}],
        },
        "finish-inspection",
    )
    current = command("plan_get", {"task": "checkout-plan", "revision": None})
    previous = command("plan_get", {"task": "checkout-plan", "revision": 1})
    listed = command("plan_list", {"limit": 10, "cursor": None})
    assert current["progress"]["completed"] == 1 and current["progress"]["total"] == 2
    assert previous["progress"]["completed"] == 0
    assert listed["plans"][0]["id"] == "checkout-plan"
    return {
        "completed": 1,
        "total": 2,
        "work_percent": 50,
        "current_phase": "verify",
        "historical_completed": 0,
    }


def main():
    with tempfile.TemporaryDirectory(prefix="mk-plans-", dir="/tmp") as directory:
        root = Path(directory)
        credential = LocalStore.initialize(root / "db", root / "ctl")
        with LocalStore(root / "db", root / "ctl") as store, Server(store) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = exercise(LocalClient(server.path, credential, "demo"))
                print("Plan progress: 1 of 2 steps completed (50%); verification remains pending.")
                print(json.dumps(result, sort_keys=True))
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    main()
