"""Run actual SIGKILL/recovery and full-context continuation with the local reference harness."""

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
from memoryking.domain.local import LocalError
from memoryking.harness import LocalFileTool, ReferenceHarness
from memoryking.harness_context import LocalRecordingProvider
from memoryking.sdk import LocalClient

ROOT = Path(__file__).resolve().parents[1]


def child(socket, credential, directory, mode, constraint, failed, project):
    client = LocalClient(Path(socket), Path(credential), project)
    root = Path(directory)
    body = json.loads((ROOT / "fixtures/contracts/checkpoint.json").read_text())

    def fault(point):
        if (mode == "effect" and point == "after_external_effect") or (
            mode == "ack" and point == "after_checkpoint_ack"
        ):
            os.kill(os.getpid(), signal.SIGKILL)

    harness = ReferenceHarness(
        client, root / "journal", LocalFileTool(root / "work"), task=mode, state=body, fault=fault
    )
    harness.accept_constraint(constraint, approved=True)
    state = harness.state()
    state["failed_attempts"].append(
        {
            "action": failed,
            "outcome": "Uncertain result",
            "do_not_repeat_when": "The outcome remains unknown",
        }
    )
    harness.update_state(state)
    if mode == "effect":
        harness.create_file(
            "file-operation", "result.json", "A real disposable artifact", approved=True
        )
    if mode == "model":
        provider = LocalRecordingProvider(root / "provider")
        dispatch = provider.dispatch

        def interrupt_after_dispatch(raw):
            dispatch(raw)
            os.kill(os.getpid(), signal.SIGKILL)

        provider.dispatch = interrupt_after_dispatch
        harness.model_turn(provider)
    os.kill(os.getpid(), signal.SIGKILL)


def kill_child(socket, credential, root, mode, constraint, failed, project="demo"):
    root.mkdir(mode=0o700, exist_ok=True)
    env = {
        key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--child",
            mode,
            "--project",
            project,
            "--socket",
            str(socket),
            "--credential",
            str(credential),
            "--directory",
            str(root),
            "--constraint",
            constraint,
            "--failed",
            failed,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != -signal.SIGKILL:
        raise RuntimeError(
            "Reference child did not reach its SIGKILL boundary: " + result.stderr[-1000:]
        )


def exercise_harness(
    store,
    credential,
    root,
    *,
    constraint="Never retry an unknown payment outcome",
    failed="blind retry",
    project="demo",
):
    with Server(store) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            client = LocalClient(server.path, credential, project)
            first_root = root / "task-state"
            kill_child(server.path, credential, first_root, "state", constraint, failed, project)
            first = ReferenceHarness(
                client, first_root / "journal", LocalFileTool(first_root / "work"), task="state"
            )
            try:
                packet = first.reset_context()
                state = packet["checkpoint"]["state"]
                preserved = constraint in state["hard_constraints"] and any(
                    item["action"] == failed for item in state["failed_attempts"]
                )
                output = first.model_turn(LocalRecordingProvider(first_root / "provider"))
                actual_request = Path(output["request_path"]).read_text()
                assert constraint in actual_request and failed in actual_request
            finally:
                first.close()
            effect_root = root / "task-effect"
            kill_child(server.path, credential, effect_root, "effect", constraint, failed, project)
            target = effect_root / "work/result.json"
            before = (target.read_bytes(), target.stat().st_ino, target.stat().st_mtime_ns)
            second = ReferenceHarness(
                client, effect_root / "journal", LocalFileTool(effect_root / "work"), task="effect"
            )
            try:
                rows = second.resume()["operations"]
                reconciled = any(
                    row["id"] == "file-operation"
                    and row["state"] == "succeeded"
                    and not row["reconciliation_required"]
                    for row in rows
                )
                denied = False
                try:
                    second.create_file(
                        "file-operation", "result.json", "A real disposable artifact", approved=True
                    )
                except LocalError as exc:
                    denied = exc.code == "operation_already_recorded"
                assert before == (
                    target.read_bytes(),
                    target.stat().st_ino,
                    target.stat().st_mtime_ns,
                )
                state = second.state()
                state["repository"].update(
                    dirty=True,
                    artifacts=[
                        {
                            "path": "missing.patch",
                            "availability": "missing",
                            "observation": None,
                            "content_hash": None,
                            "required": True,
                        }
                    ],
                )
                second.update_state(state)
                incomplete = second.resume()
                blocked = False
                try:
                    second.create_file("must-not-run", "second.json", "blocked", approved=True)
                except LocalError as exc:
                    blocked = exc.code == "harness_recovery_incomplete"
                assert not (effect_root / "work/second.json").exists()
                return {
                    "critical_constraint_retained": constraint in state["hard_constraints"],
                    "failed_attempt_retained": any(
                        item["action"] == failed for item in state["failed_attempts"]
                    ),
                    "actual_model_request_retains_essentials": constraint in actual_request
                    and failed in actual_request,
                    "acknowledged_state_recovered": preserved,
                    "missing_artifact_detected": incomplete["status"] == "incomplete"
                    and "required_artifact_not_durable" in incomplete["recovery_gaps"],
                    "ambiguous_redispatch_blocked": denied and reconciled,
                    "readback_reconciled_without_rewrite": before
                    == (target.read_bytes(), target.stat().st_ino, target.stat().st_mtime_ns),
                    "dependent_mutation_blocked": blocked,
                    "actual_processes_killed": 2,
                    "external_provider_calls": 0,
                }
            finally:
                second.close()
        finally:
            server.shutdown()
            worker.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="demo")
    parser.add_argument("--child", choices=["state", "effect", "ack", "model"])
    for name in ("socket", "credential", "directory", "constraint", "failed"):
        parser.add_argument("--" + name)
    args = parser.parse_args()
    if args.child:
        child(
            args.socket,
            args.credential,
            args.directory,
            args.child,
            args.constraint,
            args.failed,
            args.project,
        )
        return
    with tempfile.TemporaryDirectory(prefix="mk-harness-", dir="/tmp") as directory:
        root = Path(directory)
        credential = LocalStore.initialize(root / "db", root / "ctl")
        with LocalStore(root / "db", root / "ctl") as store:
            print(json.dumps(exercise_harness(store, credential, root)))


if __name__ == "__main__":
    main()
