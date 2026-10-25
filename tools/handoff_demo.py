"""Exercise an actual two-principal local SDK handoff with preserved verification gaps."""

import hashlib
import json
import tempfile
import threading
from pathlib import Path

from memoryking.adapters.local_files import atomic
from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import LocalError
from memoryking.sdk import LocalClient


def exercise_handoff(
    store,
    credential_file,
    project,
    sender="implementer",
    recipient="reviewer",
    gap="integration test not run",
):
    owner = credential_file.read_text()
    sender_token = store.grant(owner, sender, project, ["read", "checkpoint", "handoff"])
    recipient_token = store.grant(owner, recipient, project, ["read"])
    sender_file = store.control_path / "demo-sender.credential"
    recipient_file = store.control_path / "demo-recipient.credential"
    atomic(sender_file, sender_token.encode())
    atomic(recipient_file, recipient_token.encode())
    body = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures/contracts/checkpoint.json").read_text()
    )
    body["verification"][0].update(name=gap, result="not_evaluated")
    body["blockers"] = [{"text": gap, "owner": sender}]
    with Server(store) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            source = LocalClient(server.path, sender_file, project)
            target = LocalClient(server.path, recipient_file, project)
            scope = {"project": project, "task": "demo-task", "run": None}

            def command(action, args, key):
                return {
                    "version": 1,
                    "incarnation": store.incarnation,
                    "action": action,
                    "key": key,
                    "args": args,
                }

            saved = source.working(
                command(
                    "save",
                    {
                        **scope,
                        "expected_revision": 0,
                        "readers": [sender, recipient],
                        "checkpoint": body,
                    },
                    "save",
                )
            )
            prepared = source.handoff(
                command(
                    "prepare",
                    {
                        **scope,
                        "checkpoint": saved["checkpoint"],
                        "recipient": recipient,
                        "role": "review",
                        "stage": "verification",
                        "bindings": [{}],
                        "ttl_ms": 60000,
                    },
                    "prepare",
                )
            )
            view_args = {
                "project": project,
                "handle": prepared["handle"],
                "max_output_tokens": 6000,
                "tokenizer": "cl100k_base",
            }
            packet = target.handoff(command("materialize", view_args, "delivery"))
            duplicate = target.handoff(command("materialize", view_args, "delivery"))
            assert canonical_bytes(packet) == canonical_bytes(duplicate)
            assert packet["checkpoint"]["state"] == body
            assert packet["status"] == "ready"
            preview = source.handoff(command("preview", view_args, "preview"))
            assert preview["handoff"]["mode"] == "preview"
            denied = False
            try:
                source.handoff(command("materialize", view_args, "wrong-identity"))
            except LocalError as exc:
                denied = exc.code == "handoff_unavailable"
            assert denied
            ack = target.handoff(
                command(
                    "ack",
                    {
                        "project": project,
                        "handle": prepared["handle"],
                        "materialization": packet["receipt"],
                        "rendered_hash": hashlib.sha256(canonical_bytes(packet)).hexdigest(),
                    },
                    "ack",
                )
            )
            assert ack["status"] == "acknowledged" and not ack["task_complete"]
            assert (
                store.db.execute(
                    "SELECT writer FROM checkpoint_streams WHERE task=?", ("demo-task",)
                ).fetchone()[0]
                == sender
            )
            actual = packet["checkpoint"]["state"]
            return {
                "verification_gap_retained": any(
                    item["name"] == gap and item["result"] == "not_evaluated"
                    for item in actual["verification"]
                ),
                "full_essential_state": actual == body,
                "recipient_authenticated": packet["context_binding"]["principal"] == recipient,
                "wrong_identity_denied": denied,
                "duplicate_bytes_identical": canonical_bytes(packet) == canonical_bytes(duplicate),
                "ack_is_not_completion": not ack["task_complete"],
                "provider_calls": 0,
                "messages_sent": 0,
            }
        finally:
            server.shutdown()
            worker.join()


def main():
    with tempfile.TemporaryDirectory(prefix="mk-handoff-", dir="/tmp") as directory:
        root = Path(directory)
        credential = LocalStore.initialize(root / "db", root / "ctl")
        with LocalStore(root / "db", root / "ctl") as store:
            print(json.dumps(exercise_handoff(store, credential, "demo")))


if __name__ == "__main__":
    main()
