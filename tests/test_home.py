"""Home process ownership, shutdown, private configuration and socket-only PostgreSQL."""

import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_postgres_parity import NativeFixture
from test_viewer_contracts import config

from memoryking.adapters.local_files import atomic
from memoryking.adapters.local_ipc import request
from memoryking.adapters.local_store import LocalStore
from memoryking.adapters.report_worker import Configuration
from memoryking.adapters.viewer_config import load_config
from memoryking.domain.local import LocalError
from memoryking.home import socket_dsn
from memoryking.home_setup import configurations, postgres_secrets


class HomeProcess:
    """Bound startup reads and always reap child processes, including failed assertions."""

    def __init__(self, data, control, *extra):
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "memoryking.home",
                "--data",
                str(data),
                "--control",
                str(control),
                *map(str, extra),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def ready(self):
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            if not selector.select(15):
                raise AssertionError("home startup timed out")
        line = self.process.stdout.readline()
        if not line:
            raise AssertionError(self.process.communicate(timeout=5)[1])
        return json.loads(line)

    def close(self):
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        try:
            out, err = self.process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate()
            raise AssertionError("home shutdown timed out") from None
        return self.process.returncode, out, err


class HomeConfigurationTests(unittest.TestCase):
    def test_fresh_configs_disable_viewer_worker_and_sender_and_cannot_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configurations(root)
            self.assertFalse(load_config(root / "viewer.json")["enabled"])
            reports = Configuration.from_file(root / "reports.json")
            self.assertFalse(reports.enabled)
            self.assertFalse(json.loads((root / "reports.json").read_text())["sender_enabled"])
            self.assertIsNone(reports.mail)
            before = (root / "viewer.json").read_bytes()
            with self.assertRaisesRegex(LocalError, "home_configuration_exists"):
                configurations(root)
            self.assertEqual((root / "viewer.json").read_bytes(), before)

    def test_postgres_secret_roles_are_separated_from_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runtime, admin = base / "secrets", base / "admin"
            runtime.mkdir(mode=0o700)
            admin.mkdir(mode=0o700)
            postgres_secrets(runtime, admin)
            self.assertEqual({p.name for p in runtime.iterdir()}, {"runtime.dsn"})
            self.assertEqual(
                {p.name for p in (admin / "roles").iterdir()},
                {"mk_migration", "mk_runtime", "mk_worker", "mk_backup"},
            )
            for path in (*runtime.iterdir(), *(p for p in admin.rglob("*") if p.is_file())):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(LocalError, "home_database_secrets_exist"):
                postgres_secrets(runtime, admin)

    def test_socket_dsn_rejects_tcp_multi_host_service_and_environment_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dsn"
            valid = "host=/private/run/postgresql user=mk_runtime dbname=memoryking"
            atomic(path, valid.encode())
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(socket_dsn(path), valid)
                for invalid in (
                    "dbname=memoryking",
                    "host=localhost",
                    "host=127.0.0.1",
                    "host=/private/socket,localhost",
                    "host=/private/socket hostaddr=127.0.0.1",
                    "host=/private/socket service=elsewhere",
                    "host=' /tmp/socket'",
                ):
                    atomic(path, invalid.encode())
                    with self.assertRaisesRegex(LocalError, "home_database_socket_required"):
                        socket_dsn(path)
                atomic(path, valid.encode())
                for key in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"):
                    with patch.dict(os.environ, {key: "unexpected"}):
                        with self.assertRaisesRegex(LocalError, "home_database_socket_required"):
                            socket_dsn(path)
                path.chmod(0o644)
                with self.assertRaisesRegex(LocalError, "unsafe_permissions"):
                    socket_dsn(path)

    def test_invalid_configuration_fails_without_echoing_contents(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            secret = base / "viewer.json"
            atomic(secret, b'{"secret":"synthetic-do-not-log"}')
            for extra in (
                ("--viewer-config", secret),
                ("--pool-size", "1"),
                ("--backend", "shared"),
                ("--dsn-file", secret),
            ):
                child = HomeProcess(base / "data", base / "control", *extra)
                self.addCleanup(child.close)
                out, err = child.process.communicate(timeout=10)
                self.assertEqual(child.process.returncode, 1)
                self.assertNotIn("synthetic-do-not-log", out + err)
                self.assertFalse((base / "data").exists())


class HomeLocalTests(unittest.TestCase):
    def test_single_owner_private_ipc_disabled_viewer_and_restart(self):
        with tempfile.TemporaryDirectory(prefix="mk-home-", dir="/tmp") as directory:
            base = Path(directory)
            data, control = base / "data", base / "control"
            credential = LocalStore.initialize(data, control).read_text()
            viewer = base / "viewer.json"
            atomic(viewer, json.dumps(config()).encode())
            for iteration in range(2):
                child = HomeProcess(data, control, "--viewer-config", viewer)
                self.addCleanup(child.close)
                ready = child.ready()
                self.assertEqual(
                    ready,
                    {
                        "ready": True,
                        "backend": "local",
                        "viewer_enabled": False,
                        "reports_enabled": False,
                        "socket": str(data / "service.sock"),
                    },
                )
                self.assertEqual((data / "service.sock").stat().st_mode & 0o777, 0o600)
                self.assertIn(
                    "namespace",
                    request(
                        data / "service.sock", credential, "namespace", {"project": "home-test"}
                    ),
                )
                with self.assertRaisesRegex(LocalError, "owner_busy_or_unsafe"):
                    LocalStore(data, control)
                code, _, error = child.close()
                self.assertEqual((code, error), (0, ""))
                self.assertFalse((data / "service.sock").exists())
                with LocalStore(data, control) as store:
                    self.assertIsInstance(store.status(credential, "home-test"), dict)


class HomePostgresTests(NativeFixture):
    def test_missing_control_history_fails_before_ready_even_with_viewer_disabled(self):
        dsn = self.root / "runtime.dsn"
        atomic(dsn, self.cluster.dsn("mk_runtime").encode())
        child = HomeProcess(
            self.data, self.root / "absent-control", "--backend", "shared", "--dsn-file", dsn
        )
        self.addCleanup(child.close)
        output, error = child.process.communicate(timeout=10)
        self.assertEqual(child.process.returncode, 1)
        self.assertFalse(output)
        self.assertEqual(json.loads(error), {"error": "home_startup_or_service_failed"})

    def test_runtime_pool_uses_only_private_socket_and_restarts(self):
        dsn = self.root / "runtime.dsn"
        atomic(dsn, self.cluster.dsn("mk_runtime").encode())
        viewer = self.root / "viewer.json"
        atomic(viewer, json.dumps({**config(), "tenant": self.tenant}).encode())
        for iteration in range(2):
            child = HomeProcess(
                self.data,
                self.control,
                "--backend",
                "shared",
                "--dsn-file",
                dsn,
                "--viewer-config",
                viewer,
            )
            self.addCleanup(child.close)
            ready = child.ready()
            self.assertEqual(
                ready,
                {
                    "ready": True,
                    "backend": "shared",
                    "viewer_enabled": False,
                    "reports_enabled": False,
                    "socket": None,
                },
            )
            self.assertFalse((self.data / "service.sock").exists())
            self.assertEqual(child.close(), (0, "", ""))


if __name__ == "__main__":
    unittest.main()
