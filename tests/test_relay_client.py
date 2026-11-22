"""The client end of the relay, against stand-in relay processes, on every platform."""

from __future__ import annotations

import inspect
import re
import sys
import textwrap
import unittest

from memoryking import __version__, relay, relay_client
from memoryking.adapters import local_ipc
from memoryking.domain.local import LocalError
from memoryking.relay_client import RelayCredential, RelayEndpoint, credential_token, relay_argv


def fake(body: str) -> list[str]:
    """A stand-in relay: a Python process that runs `body` with stdin and stdout bound."""
    prelude = "import sys, json\nout = sys.stdout.buffer\ninp = sys.stdin.buffer\n"
    return [sys.executable, "-c", prelude + textwrap.dedent(body)]


HELLO = f'out.write(b\'{{"relay":1,"version":"{__version__}","credential":"tok-1"}}\\n\'); out.flush()\n'
ECHO = """
for line in iter(inp.readline, b""):
    request = json.loads(line)
    out.write(json.dumps({"ok": True, "seen": request}).encode() + b"\\n"); out.flush()
"""


class RelayClientTests(unittest.TestCase):
    def fails(self, code: str, call: object) -> LocalError:
        with self.assertRaises(LocalError) as caught:
            call()  # type: ignore[operator]
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_limits_match_the_daemon_the_client_and_the_relay(self) -> None:
        self.assertEqual(relay_client.REQUEST_LIMIT, local_ipc.WIRE_LIMIT)
        self.assertEqual(relay_client.REQUEST_LIMIT, relay.REQUEST_LIMIT)
        self.assertEqual(relay_client.RESPONSE_LIMIT, relay.RESPONSE_LIMIT)
        self.assertEqual(relay_client.REPLY_DETAIL, local_ipc.REPLY_DETAIL)
        self.assertEqual(relay_client.PROTOCOL, relay.PROTOCOL)
        reads = re.findall(r"response\.readline\((\d+)\)", inspect.getsource(local_ipc.request))
        self.assertEqual(reads, [str(relay_client.RESPONSE_LIMIT)])

    def test_one_relay_carries_many_requests_and_the_token_stays_in_memory(self) -> None:
        with RelayEndpoint(fake(HELLO + ECHO)) as endpoint:
            credential = RelayCredential(endpoint)
            self.assertEqual(credential_token(credential), "tok-1")
            self.assertNotIn("tok-1", repr(credential) + str(credential))
            for n in range(3):
                reply = local_ipc.request(endpoint, credential.token, "context", {"n": n})
                self.assertEqual(
                    reply["seen"],
                    {"version": 1, "credential": "tok-1", "operation": "context", "args": {"n": n}},
                )

    def test_a_relay_of_another_version_is_refused_before_any_request(self) -> None:
        other = 'out.write(b\'{"relay":1,"version":"0.0.0"}\\n\'); out.flush()\n'
        refused = self.fails("relay_version_mismatch", lambda: RelayEndpoint(fake(other + ECHO)))
        self.assertEqual(refused.detail, {"expected": __version__, "found": "0.0.0"})

    def test_a_refusing_relay_names_its_reason(self) -> None:
        body = 'out.write(b\'{"error":"unsafe_socket","hint":"x"}\\n\'); out.flush()\n'
        self.fails("unsafe_socket", lambda: RelayEndpoint(fake(body)))

    def test_refusals_from_the_daemon_keep_their_code_hint_and_public_detail(self) -> None:
        body = HELLO + textwrap.dedent(
            """
            for line in iter(inp.readline, b""):
                out.write(b'{"error":"denied","hint":"h","required_capability":"capture","secret":"s"}\\n')
                out.flush()
            """
        )
        with RelayEndpoint(fake(body)) as endpoint:
            refused = self.fails("denied", lambda: local_ipc.request(endpoint, "t", "x", {}))
            self.assertEqual(refused.detail, {"required_capability": "capture"})

    def test_a_relay_that_ends_after_the_request_is_an_unknown_outcome(self) -> None:
        body = HELLO + "inp.readline()\n"  # reads the request, then exits without replying
        with RelayEndpoint(fake(body)) as endpoint:
            self.fails("relay_interrupted", lambda: local_ipc.request(endpoint, "t", "x", {}))
            self.fails("relay_unavailable", lambda: local_ipc.request(endpoint, "t", "x", {}))

    def test_no_relay_at_all(self) -> None:
        self.fails("relay_unavailable", lambda: RelayEndpoint(["memoryking-no-such-program"]))
        self.fails("relay_unavailable", lambda: RelayEndpoint(fake("pass\n")))

    def test_an_oversized_request_never_leaves(self) -> None:
        with RelayEndpoint(fake(HELLO + ECHO)) as endpoint:
            self.fails(
                "invalid_request",
                lambda: local_ipc.request(endpoint, "t", "x", {"a": "b" * relay.REQUEST_LIMIT}),
            )

    def test_the_relay_command_is_explicit_and_needs_no_shell(self) -> None:
        self.assertEqual(
            relay_argv("wsl:Ubuntu-24.04", "/home/u/s.sock", "/home/u/c", None),
            [
                "wsl.exe", "-d", "Ubuntu-24.04", "--exec", "python3", "-m", "memoryking.relay",
                "--socket", "/home/u/s.sock", "--credential", "/home/u/c",
            ],
        )  # fmt: skip
        self.assertEqual(
            relay_argv("docker:memoryking", "/data/store/service.sock", None, "/opt/py"),
            [
                "docker", "exec", "-i", "memoryking", "/opt/py", "-m", "memoryking.relay",
                "--socket", "/data/store/service.sock",
            ],
        )  # fmt: skip
        for spec, path in (
            ("ssh:host", "/s"),
            ("wsl:", "/s"),
            ("wsl:bad name", "/s"),
            ("wsl:-d", "/s"),
            ("docker:c", "relative/s"),
            ("docker:c", "C:\\s"),
        ):
            with self.subTest(spec=spec, path=path):
                self.fails("relay_invalid", lambda s=spec, p=path: relay_argv(s, p, None, None))


if __name__ == "__main__":
    unittest.main()
