"""Real local/shared CLI startup and shutdown with no production login bypass."""

import json
import signal
import socket
import sys
import unittest

from test_postgres_parity import NativeFixture
from test_viewer_contracts import config

from memoryking.adapters.postgres_store import PostgresStore
from tools.viewer_demo import free_port, rehearse


class SharedViewerStartup(NativeFixture):
    def test_shared_cli_omitted_disabled_enabled_and_shutdown(self):
        self.store.close()
        dsn = self.root / "runtime.dsn"
        dsn.write_text(self.cluster.dsn("mk_runtime"))
        dsn.chmod(0o600)
        agent_port = free_port()
        settings = {**config(), "tenant": self.tenant, "listen_port": free_port()}

        def ready():
            try:
                with socket.create_connection(("127.0.0.1", agent_port), timeout=0.2):
                    return True
            except OSError:
                return False

        try:
            result = rehearse(
                [
                    sys.executable,
                    "-m",
                    "memoryking.shared",
                    "serve",
                    "--dsn-file",
                    str(dsn),
                    "--content",
                    str(self.data),
                    "--control",
                    str(self.control),
                    "--port",
                    str(agent_port),
                    "--host",
                    f"127.0.0.1:{agent_port}",
                    "--loopback-http",
                ],
                settings,
                self.root / "viewer.json",
                ready,
                exit_codes=(0, -signal.SIGTERM),
            )
            self.assertEqual(len(result), 3)
            self.assertTrue(all(r["passed"] for r in result))
        finally:
            self.store = PostgresStore(self.pool, self.data, self.control, self.tenant, self.owner)


class LocalViewerStartup(unittest.TestCase):
    def test_documented_local_example(self):
        import subprocess

        result = subprocess.run(
            [sys.executable, "tools/viewer_demo.py"], capture_output=True, text=True, timeout=40
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["viewer_shell_only"])
