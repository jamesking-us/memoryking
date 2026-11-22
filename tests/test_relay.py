"""`memoryking relay` carries request lines to a real daemon and back, unchanged."""

from __future__ import annotations

import inspect
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from memoryking import __version__, relay
from memoryking.adapters import local_ipc
from memoryking.adapters.platform_support import LOCAL_STORE_SUPPORTED
from memoryking.contracts.canonical import canonical_bytes


def raw_request(path: Path, line: bytes) -> bytes:
    """What a local client sees on the socket for one request line."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(10)
        stream.connect(str(path))
        stream.sendall(line)
        with stream.makefile("rb") as response:
            return response.readline(relay.RESPONSE_LIMIT)


class LimitTests(unittest.TestCase):
    def test_the_relay_uses_the_daemons_and_the_clients_limits(self) -> None:
        self.assertEqual(relay.REQUEST_LIMIT, local_ipc.WIRE_LIMIT)
        reads = re.findall(r"response\.readline\((\d+)\)", inspect.getsource(local_ipc.request))
        self.assertEqual(reads, [str(relay.RESPONSE_LIMIT)])


@unittest.skipUnless(
    LOCAL_STORE_SUPPORTED, "the relay runs beside the daemon, where the store is (not Windows)"
)
class RelayTests(unittest.TestCase):
    def setUp(self) -> None:
        from memoryking.adapters.local_store import LocalStore

        self.temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-relay-")
        root = Path(self.temp.name)
        self.credential = LocalStore.initialize(root / "data", root / "control")
        self.token = self.credential.read_text(encoding="utf-8").strip()
        self.store = LocalStore(root / "data", root / "control")
        self.start()

    def start(self, **limits: int) -> None:
        self.server = local_ipc.Server(self.store, **limits)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def tearDown(self) -> None:
        self.stop()
        self.store.close()
        self.temp.cleanup()

    def line(self, operation: str, args: dict[str, object], credential: str | None = None) -> bytes:
        body = {
            "version": 1,
            "credential": self.token if credential is None else credential,
            "operation": operation,
            "args": args,
        }
        return canonical_bytes(body) + b"\n"

    def relayed(self, *lines: bytes, credential: Path | None = None) -> list[bytes]:
        out = io.BytesIO()
        code = relay.serve(self.server.path, credential, io.BytesIO(b"".join(lines)), out)
        self.assertEqual(code, 0)
        return out.getvalue().splitlines(keepends=True)

    def test_handshake_names_the_version_and_hands_over_the_credential(self) -> None:
        first = json.loads(self.relayed(credential=self.credential)[0])
        self.assertEqual(first, {"relay": 1, "version": __version__, "credential": self.token})
        self.assertEqual(json.loads(self.relayed()[0]), {"relay": 1, "version": __version__})

    def test_replies_are_the_daemons_bytes(self) -> None:
        denied = self.line("capture", {"project": "p", "text": "x"}, credential="not-a-token")
        self.assertEqual(self.relayed(denied)[1:], [raw_request(self.server.path, denied)])
        observation = {
            "version": 1,
            "project": "p",
            "source": "relay",
            "source_event": "e1",
            "text": "relayed text arrives",
            "readers": None,
            "classification": "internal",
            "observed_at_ms": 1_700_000_000_000,
        }
        namespace = json.loads(
            raw_request(self.server.path, self.line("namespace", {"project": "p", "name": "relay"}))
        )["namespace"]
        capture = self.line(
            "capture", {"namespace": namespace, "key": "k1", "request": observation}
        )
        first = self.relayed(capture, capture)[1:]
        self.assertEqual(len(first), 2)
        self.assertEqual(first[0], first[1])  # the same event replays the same reply
        self.assertEqual(first[0], raw_request(self.server.path, capture))
        self.assertIn("observation", json.loads(first[0]))

    def test_an_over_long_line_is_refused_by_the_daemon_and_the_next_line_still_works(
        self,
    ) -> None:
        huge = b'{"x":"' + b"a" * (relay.REQUEST_LIMIT * 2) + b'"}\n'
        status = self.line("context", {"project": "p"})
        replies = self.relayed(huge, status)
        self.assertEqual(len(replies), 3)
        self.assertEqual(json.loads(replies[1])["error"], "invalid_request")
        self.assertNotIn("error", json.loads(replies[2]))

    def test_service_busy_passes_through(self) -> None:
        self.stop()
        self.start(workers=1, waiters=0)
        holder = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        holder.connect(str(self.server.path))  # occupies the one worker until it times out
        try:
            reply = None
            for _ in range(50):
                reply = json.loads(self.relayed(self.line("context", {"project": "p"}))[1])
                if reply.get("error") == "service_busy":
                    break
            self.assertEqual(reply and reply.get("error"), "service_busy")
        finally:
            holder.close()

    def test_no_daemon_is_transport_unavailable_and_the_relay_keeps_serving(self) -> None:
        self.stop()
        status = self.line("context", {"project": "p"})
        replies = self.relayed(status, status)
        self.assertEqual(
            [json.loads(r)["error"] for r in replies[1:]], ["transport_unavailable"] * 2
        )
        self.start()

    def test_refuses_a_path_that_is_not_its_socket_and_a_shared_credential(self) -> None:
        impostor = Path(self.temp.name) / "impostor.sock"
        impostor.write_bytes(b"")
        out = io.BytesIO()
        with self.assertRaises(Exception) as caught:
            relay.serve(impostor, None, io.BytesIO(b""), out)
        self.assertEqual(getattr(caught.exception, "code", None), "unsafe_socket")
        self.assertEqual(out.getvalue(), b"")
        shared = Path(self.temp.name) / "shared.credential"
        shared.write_text(self.token, encoding="utf-8")
        os.chmod(shared, 0o644)
        with self.assertRaises(Exception) as caught:
            relay.serve(self.server.path, shared, io.BytesIO(b""), io.BytesIO())
        self.assertEqual(getattr(caught.exception, "code", None), "unsafe_permissions")

    def test_the_command_over_real_pipes(self) -> None:
        status = self.line("context", {"project": "p"})
        result = subprocess.run(
            [sys.executable, "-m", "memoryking.relay", "--socket", str(self.server.path)],
            input=status + status,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines(keepends=True)
        self.assertEqual(json.loads(lines[0])["relay"], 1)
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[1]).get("ok", True), json.loads(lines[2]).get("ok", True))
        bad = subprocess.run(
            [sys.executable, "-m", "memoryking.relay", "--socket", self.temp.name],
            input=b"",
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(json.loads(bad.stdout)["error"], "unsafe_socket")


@unittest.skipUnless(
    LOCAL_STORE_SUPPORTED, "the relay runs beside the daemon, where the store is (not Windows)"
)
class RelayProfileTests(unittest.TestCase):
    """`relay --profile`: the profile's socket and credential, and the daemon on demand."""

    def setUp(self) -> None:
        from memoryking import profiles
        from memoryking.adapters.local_store import LocalStore

        self.temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-relayp-")
        root = Path(self.temp.name)
        self.credential = LocalStore.initialize(root / "data", root / "control")
        self.config = root / "home" / "config.toml"
        self.values = {
            "store": str(root / "data"),
            "control": str(root / "control"),
            "credential": str(self.credential),
            "autostart": True,
        }
        profiles.save("relayed", self.values, self.config)

    def tearDown(self) -> None:
        from memoryking import daemon, profiles

        try:
            daemon.stop(profiles.resolve("relayed", self.config))
        finally:
            self.temp.cleanup()

    def relay(self, *lines: bytes) -> list[bytes]:
        result = subprocess.run(
            [
                sys.executable, "-m", "memoryking.relay",
                "--profile", "relayed", "--config", str(self.config),
            ],
            input=b"".join(lines),
            capture_output=True,
            timeout=120,
        )  # fmt: skip
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines(keepends=True)

    def context(self, token: str) -> bytes:
        body = {"version": 1, "credential": token, "operation": "context", "args": {"project": "p"}}
        return canonical_bytes(body) + b"\n"

    def test_no_daemon_is_started_on_demand_and_the_profiles_credential_is_handed_over(
        self,
    ) -> None:
        socket_path = Path(self.values["store"]) / "service.sock"
        self.assertFalse(socket_path.exists())
        token = self.credential.read_text(encoding="utf-8").strip()
        hello, reply = self.relay(self.context(token))
        self.assertEqual(json.loads(hello)["credential"], token)
        self.assertNotIn("error", json.loads(reply))
        self.assertTrue(socket_path.exists())

    def test_a_profile_without_autostart_says_how_to_start(self) -> None:
        from memoryking import profiles

        profiles.save("relayed", {**self.values, "autostart": False}, self.config)
        (first,) = self.relay()
        self.assertEqual(json.loads(first)["error"], "transport_unavailable")


if __name__ == "__main__":
    unittest.main()
