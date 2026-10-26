"""Build a synthetic draft locally. No email, publication or fabricated visual QA."""

import base64
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from memoryking.adapters.local_files import atomic
from memoryking.adapters.local_store import LocalStore
from memoryking.sdk import LocalClient


def main():
    with tempfile.TemporaryDirectory(prefix="mk-report-example-") as directory:
        root = Path(directory)
        credential = LocalStore.initialize(root / "data", root / "control")
        with LocalStore(root / "data", root / "control") as store:
            token = store.grant(
                credential.read_text(), "report-worker", "example", ["read", "report_author"]
            )
            atomic(root / "worker.credential", token.encode())
        atomic(
            root / "worker.json",
            json.dumps(
                {
                    "version": 1,
                    "enabled": True,
                    "tenant": "main",
                    "project": "example",
                    "credential_file": str(root / "worker.credential"),
                    "mail_secret_file": None,
                    "sender_enabled": False,
                    "poll_seconds": 1,
                }
            ).encode(),
        )
        with (root / "service.log").open("wb") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "memoryking",
                    "serve",
                    "--store",
                    str(root / "data"),
                    "--control",
                    str(root / "control"),
                    "--report-config",
                    str(root / "worker.json"),
                ],
                stdout=log,
                stderr=log,
            )
            try:
                deadline = time.monotonic() + 10
                while not (root / "data/service.sock").exists():
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Example service did not start")
                    time.sleep(0.02)
                client = LocalClient(root / "data/service.sock", credential, "example")
                serial = 0

                def command(action, **args):
                    nonlocal serial
                    serial += 1
                    return client.report(
                        {
                            "version": 1,
                            "incarnation": client.binding[-1],
                            "key": f"example-{serial}",
                            "action": action,
                            "args": {"project": "example", **args},
                        }
                    )

                command(
                    "policy",
                    expected_version=0,
                    policy={
                        "version": 1,
                        "project": "example",
                        "program": "example",
                        "template": "phase-report",
                        "channel": "app",
                        "destination": None,
                        "recipient_principal": None,
                        "require_visual_qa": True,
                        "require_evidence": True,
                        "classification": "internal",
                        "readers": ["owner", "report-worker"],
                    },
                )
                source = "# Synthetic report example\n\n" + "\n\n".join(
                    f"## {number}. Example section {number}\n\nSynthetic pipeline example; no implementation or visual review is claimed."
                    for number in range(1, 18)
                )
                files = {
                    "source.md": source.encode(),
                    "readme.md": b"# Synthetic example README",
                    "evidence.json": b'{"scope":"synthetic pipeline example"}',
                }
                dependencies = []
                for name, data in files.items():
                    digest = hashlib.sha256(data).hexdigest()
                    if name != "source.md":
                        binding_id = name.split(".")[0]
                        command(
                            "bind_source",
                            expected_version=0,
                            binding={
                                "id": binding_id,
                                "sha256": digest,
                                "readers": ["owner", "report-worker"],
                                "classification": "internal",
                                "enabled": True,
                                "starts": None,
                                "expires": None,
                            },
                        )
                        dependencies.append(
                            {"kind": "external_source", "id": binding_id, "sha256": digest}
                        )
                    command(
                        "upload",
                        upload="example-inputs",
                        name=name,
                        sha256=digest,
                        bytes=len(data),
                        part=0,
                        parts=1,
                        data_base64=base64.b64encode(data).decode(),
                    )
                command(
                    "submit",
                    report="example-report",
                    expected_revision=0,
                    upload="example-inputs",
                    program="example",
                    phase="EXAMPLE",
                    task=None,
                    title="Synthetic report example",
                    technical_status="partial",
                    classification="internal",
                    dependencies=dependencies,
                )
                command("build", report="example-report", revision=1)
                deadline = time.monotonic() + 15
                while True:
                    status = command("status", report="example-report", revision=1)
                    if status["state"] == "built":
                        break
                    if status["state"] == "failed" or time.monotonic() >= deadline:
                        raise RuntimeError("Example report build failed or timed out")
                    time.sleep(0.05)
                print(
                    json.dumps(
                        {
                            "state": status["state"],
                            "artifacts": status["artifacts"],
                            "qa": status["qa"],
                            "published": False,
                            "email_sent": False,
                        },
                        indent=2,
                    )
                )
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise


if __name__ == "__main__":
    main()
