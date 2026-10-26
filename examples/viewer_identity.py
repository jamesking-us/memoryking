"""Disposable owner-channel demo. No GitHub login, public listener, or live data."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
from memoryking.adapters.viewer_identity import ViewerIdentity
from memoryking.domain.local import LocalError


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="mk-viewer-demo-", dir="/tmp") as temporary:
        root = Path(temporary)
        owner = LocalStore.initialize(root / "data", root / "control").read_text()
        with LocalStore(root / "data", root / "control") as store:
            server = Server(store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:

                def policy(action: str, args: dict, key: str) -> dict:
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "memoryking",
                            "viewer-admin",
                            "--socket",
                            str(store.root / "service.sock"),
                            "--credential",
                            str(root / "control/owner.credential"),
                        ],
                        input=json.dumps(
                            {
                                "version": 1,
                                "incarnation": store.incarnation,
                                "key": key,
                                "action": action,
                                "args": args,
                            }
                        ),
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    return json.loads(result.stdout)

                policy(
                    "bind",
                    {"principal": "phone", "issuer": "https://github.com", "user_id": "12345"},
                    "demo-bind",
                )
                policy(
                    "grant",
                    {
                        "principal": "phone",
                        "project": "demo",
                        "expected_version": 1,
                        "starts": None,
                        "expires": None,
                    },
                    "demo-grant",
                )
                namespace = store.namespace(owner, "demo")
                for key, text, readers in (
                    ("shared", "A shared project finding", None),
                    ("private", "Owner-only note", ["owner"]),
                ):
                    store.capture(
                        owner,
                        namespace,
                        key,
                        {
                            "version": 1,
                            "project": "demo",
                            "source": "example",
                            "source_event": key,
                            "text": text,
                            "readers": readers,
                            "classification": "internal",
                            "observed_at_ms": 0,
                        },
                    )
                # Simulate an already-verified provider identity inside the trusted process.
                # A browser must not be allowed to select this subject itself.
                subject = ViewerIdentity(store).resolve_authenticated_subject(
                    "https://github.com", "12345"
                )
                output = []
                store.read(subject.credential, "demo").emit(output.append)
                result = json.loads(output[0])
                assert len(result["items"]) == 1
                assert result["items"][0]["text"] == "A shared project finding"
                policy(
                    "revoke",
                    {"principal": "phone", "project": "demo", "expected_version": 2},
                    "demo-revoke",
                )
                try:
                    store.read(subject.credential, "demo")
                except LocalError as error:
                    assert error.code == "denied"
                else:
                    raise AssertionError("revocation did not hold")
                print(
                    "Viewer identity demo: explicit grant, owner-only isolation and revocation verified."
                )
                print(
                    "Disposable SQLite and owner IPC only; real GitHub login and phone UI are not configured."
                )
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()


if __name__ == "__main__":
    main()
