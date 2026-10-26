"""Fixed provider transport, hostile responses, isolation and private secret files."""

import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from memoryking.adapters import github_oauth as parent
from memoryking.adapters import github_oauth_worker as worker
from memoryking.domain.local import LocalError


class SocketBytes:
    def __init__(self, raw):
        self.raw = raw

    def makefile(self, mode):
        return io.BytesIO(self.raw)


class ProviderAdapterTests(unittest.TestCase):
    def fetch(self, raw):
        class Connection:
            response_class = worker.BoundedResponse

            def request(self, *args, **kwargs):
                pass

            def getresponse(self):
                response = self.response_class(SocketBytes(raw))
                response.begin()
                return response

            def close(self):
                pass

        with patch.object(worker.http.client, "HTTPSConnection", return_value=Connection()) as call:
            result = worker.fetch("github.com", "/login/oauth/access_token", b"synthetic", {})
            self.assertEqual(call.call_args.args, ("github.com",))
            self.assertIsNotNone(call.call_args.kwargs["context"])
            return result

    def test_response_limits_duplicates_redirects_scopes_and_json(self):
        good = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
        self.assertEqual(self.fetch(good), {})
        for raw in (
            good.replace(b"200 OK", b"302 Found"),
            good.replace(b"application/json", b"text/html"),
            good.replace(b"Content-Length: 2", b"Content-Length: 65537"),
            good.replace(b"Content-Length: 2", b"Content-Length: 2\r\nContent-Length: 2"),
            good.replace(b"Content-Length: 2", b"Content-Encoding: gzip"),
            good.replace(b"Content-Length: 2", b"X-OAuth-Scopes: repo"),
            good.replace(b"Content-Length: 2", b"X-Huge: " + b"a" * 16384),
            good.replace(b"Content-Length: 2", b"X-Header: a\r\n" * 65),
            good.replace(b"Content-Length: 2\r\n", b"").replace(b"{}", b"a" * 65537),
            good.replace(b"Content-Length: 2\r\n", b"").replace(b"{}", b'{"id":1,"id":2}'),
        ):
            with (
                self.subTest(size=len(raw)),
                self.assertRaises((ValueError, http.client.HTTPException)),
            ):
                self.fetch(raw)

    def exchange(self, token=None, user=None):
        token = (
            token
            if token is not None
            else {"access_token": "synthetic-provider-token", "scope": "", "token_type": "bearer"}
        )
        user = (
            user
            if user is not None
            else {"id": 12345, "type": "User", "login": "rename-does-not-matter"}
        )
        with patch.object(worker, "fetch", side_effect=[token, user]) as fetch:
            result = worker.exchange(
                {
                    "client_id": "synthetic-client",
                    "client_secret": "synthetic-private-secret",
                    "code": "synthetic-code",
                    "code_verifier": "synthetic-verifier",
                    "redirect_uri": "https://viewer.example.invalid/viewer/auth/github/callback",
                }
            )
            self.assertEqual(
                [c.args[:2] for c in fetch.call_args_list],
                [("github.com", "/login/oauth/access_token"), ("api.github.com", "/user")],
            )
            self.assertEqual(
                fetch.call_args_list[1].args[3]["Authorization"], "Bearer synthetic-provider-token"
            )
            return result

    def test_fixed_targets_empty_scopes_numeric_id_and_token_discard(self):
        self.assertEqual(self.exchange(), "12345")
        for token in (
            {"scope": "repo", "token_type": "bearer", "access_token": "synthetic"},
            {"scope": "", "token_type": "mac", "access_token": "synthetic"},
            {"scope": "", "token_type": "bearer", "access_token": "bad\r\nheader"},
            {"scope": None, "token_type": "bearer", "access_token": "synthetic"},
        ):
            with self.assertRaises(ValueError):
                self.exchange(token=token)
        for user in (
            {"id": "12345", "type": "User"},
            {"id": True, "type": "User"},
            {"id": 0, "type": "User"},
            {"id": 12345, "type": "Organization"},
            {"id": 9007199254740992, "type": "User"},
        ):
            with self.assertRaises(ValueError):
                self.exchange(user=user)

    def test_child_total_deadline_reaps_process_without_secrets_in_args(self):
        real_popen = subprocess.Popen
        children = []

        def launch(args, **kwargs):
            self.assertNotIn("synthetic-private-secret", repr(args))
            self.assertEqual(kwargs["env"], {})
            self.assertIn("-I", args)
            child = real_popen(
                [sys.executable, "-I", "-c", "import time; time.sleep(10)"], **kwargs
            )
            children.append(child)
            return child

        provider = parent.GitHubProvider("synthetic-client", "synthetic-private-secret")
        started = time.monotonic()
        with (
            patch.object(parent.subprocess, "Popen", side_effect=launch),
            self.assertRaisesRegex(LocalError, "service_unavailable"),
        ):
            provider.authenticate(
                "synthetic-code",
                "synthetic-verifier",
                "https://viewer.example.invalid/viewer/auth/github/callback",
                time.monotonic() + 0.1,
            )
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIsNotNone(children[0].poll())

    def test_child_strict_output_spawn_failure_and_quiet_invalid_input(self):
        real_popen = subprocess.Popen
        for output in (
            b'{"user_id":"12345"}',
            b'{"user_id":12345}',
            b'{"user_id":"12345","token":"secret"}',
            b"x" * 257,
        ):

            def launch(args, **kwargs):
                return real_popen(
                    [
                        sys.executable,
                        "-I",
                        "-c",
                        "import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write("
                        + repr(output)
                        + ")",
                    ],
                    **kwargs,
                )

            with patch.object(parent.subprocess, "Popen", side_effect=launch):
                provider = parent.GitHubProvider("synthetic-client", "synthetic-private-secret")
                if output == b'{"user_id":"12345"}':
                    self.assertEqual(
                        provider.authenticate(
                            "code",
                            "verifier",
                            "https://viewer.example.invalid",
                            time.monotonic() + 2,
                        ),
                        "12345",
                    )
                else:
                    with self.assertRaises(LocalError):
                        provider.authenticate(
                            "code",
                            "verifier",
                            "https://viewer.example.invalid",
                            time.monotonic() + 2,
                        )
        with (
            patch.object(
                parent.subprocess, "Popen", side_effect=OSError("synthetic-private-secret")
            ),
            self.assertRaisesRegex(LocalError, "^service_unavailable$"),
        ):
            provider.authenticate(
                "code", "verifier", "https://viewer.example.invalid", time.monotonic() + 2
            )
        result = subprocess.run(
            [sys.executable, "-I", "-m", "memoryking.adapters.github_oauth_worker"],
            input=json.dumps({"secret": "synthetic-private-secret"}).encode(),
            capture_output=True,
            timeout=5,
            env={},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")

    def test_private_regular_secret_files_and_no_blocking_fifo(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret"
            path.write_text("synthetic-private-secret\n")
            path.chmod(0o600)
            self.assertEqual(parent.secret_file(path), "synthetic-private-secret")
            path.chmod(0o644)
            with self.assertRaises(LocalError):
                parent.secret_file(path)
            path.chmod(0o600)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(LocalError):
                parent.secret_file(link)
            path.write_text("a" * 256 + "\nextra")
            with self.assertRaises(LocalError):
                parent.secret_file(path)
            fifo = Path(directory) / "fifo"
            os.mkfifo(fifo, 0o600)
            with self.assertRaises(LocalError):
                parent.secret_file(fifo)
