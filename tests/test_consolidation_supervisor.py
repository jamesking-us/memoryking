"""The daemon schedules consolidation through supervised child processes and never reads a key."""

import http.server
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from test_consolidation_cli import Provider

from memoryking.adapters.local_files import atomic
from memoryking.adapters.local_store import LocalStore
from memoryking.consolidation import ConsolidationSupervisor

KEY_NAME = "provider-api.key"
KEY_VALUE = "sk-test-" + "k" * 24

# The daemon runs under an audit hook that records any open of the key file; the hook is in
# this process only, so a child the daemon launches is not audited. Seconds are accepted for
# the schedule here only so a pass happens within the test.
DAEMON = """
import os, sys
log = os.open(os.environ["MK_OPEN_LOG"], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
name = sys.argv[1]

def hook(kind, args):
    if kind == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        if os.fsdecode(args[0]).endswith(name):
            os.write(log, b"opened\\n")

sys.addaudithook(hook)
if os.environ.get("MK_CANARY"):
    open(os.environ["MK_CANARY"]).read()
import memoryking.__main__ as m
m.DURATION_UNITS["s"] = 1000
sys.argv = ["memoryking", *sys.argv[2:]]
sys.exit(m.main())
"""


class KeyedProvider(Provider):
    def do_POST(self):
        self.server.authorizations.append(self.headers.get("Authorization"))
        # Test-only slowness: the reply, and so the drafts, arrive after the request does.
        time.sleep(float(os.environ.get("MK_PROVIDER_DELAY", "1.0")))
        super().do_POST()


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def wait_for(condition, seconds=30.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.1)
    return condition()


class SupervisorTests(unittest.TestCase):
    def test_it_waits_an_interval_runs_a_child_restarts_failures_and_leaves_none_behind(self):
        with tempfile.TemporaryDirectory(prefix="mk-sv-", dir="/tmp") as temp:
            marks = Path(temp) / "runs"

            def command(project):
                code = "import sys; open(sys.argv[1], 'a').write('x'); sys.exit(int(sys.argv[2]))"
                return [sys.executable, "-c", code, str(marks), "1"]

            failing = ConsolidationSupervisor(command, ["a"], every_s=30.0, poll_s=0.05)
            failing.BACKOFF_FIRST_S = 0.2
            failing.start()
            try:
                self.assertEqual("waiting", failing.describe()[0]["state"])
                self.assertEqual(0, failing.describe()[0]["runs"])
                failing.due["a"] = time.monotonic()  # the interval has passed
                self.assertTrue(wait_for(lambda: failing.describe()[0]["runs"] >= 2, 10))
                described = failing.describe()[0]
                self.assertGreaterEqual(described["consecutive_failures"], 1)
                self.assertEqual(1, described["last_exit"])
                self.assertIn(described["state"], {"backoff", "running"})
            finally:
                failing.stop()

            def sleeper(project):
                return [sys.executable, "-c", "import time; time.sleep(120)"]

            running = ConsolidationSupervisor(sleeper, ["a", "b"], every_s=30.0, poll_s=0.05)
            running.start()
            running.due["a"] = running.due["b"] = time.monotonic()
            states = wait_for(
                lambda: (d := running.describe()) and all(i["pid"] for i in d) and d, 10
            )
            self.assertTrue(states)
            self.assertEqual({"running"}, {item["state"] for item in states})
            pids = [item["pid"] for item in states]
            self.assertEqual(2, len(set(pids)))
            self.assertTrue(all(alive(pid) for pid in pids))
            running.stop()
            self.assertFalse(any(alive(pid) for pid in pids))

    def test_a_child_ends_when_the_process_that_launched_it_is_killed(self):
        middle = (
            "import subprocess, sys;"
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import os, time; from memoryking.consolidation import watch_parent; "
            "watch_parent(os.getppid()); print(os.getpid(), flush=True); time.sleep(120)'], "
            "stdout=subprocess.PIPE, text=True);"
            "print(child.stdout.readline().strip(), flush=True); child.wait()"
        )
        launcher = subprocess.Popen(
            [sys.executable, "-c", middle], stdout=subprocess.PIPE, text=True
        )
        child = None
        try:
            child = int(launcher.stdout.readline())
            self.assertTrue(alive(child))
            launcher.kill()  # a daemon that dies without cleaning up
            launcher.wait()
            self.assertTrue(wait_for(lambda: not alive(child), 10))
        finally:
            launcher.kill()
            launcher.wait()
            launcher.stdout.close()
            if child is not None and alive(child):
                os.kill(child, signal.SIGKILL)


class DaemonKeyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mk-sd-", dir="/tmp")
        self.base = Path(self.temp.name)
        self.owner_file = LocalStore.initialize(self.base / "store", self.base / "control")
        self.provider = http.server.ThreadingHTTPServer(("127.0.0.1", 0), KeyedProvider)
        self.provider.authorizations = []
        threading.Thread(target=self.provider.serve_forever, daemon=True).start()
        secrets = self.base / "secrets"
        secrets.mkdir(mode=0o700)
        self.key = secrets / KEY_NAME
        atomic(self.key, KEY_VALUE.encode())
        self.policy = secrets / "egress.toml"
        atomic(
            self.policy,
            f"""revision = "sv-test"
[[providers]]
name = "openai-chat"
host = "127.0.0.1:{self.provider.server_address[1]}"
insecure_http = true
models = ["grok-4"]
key_file = "{self.key}"
""".encode(),
        )
        self.log = self.base / "opens.log"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORYKING_")}
        self.env["MK_OPEN_LOG"] = str(self.log)
        self.daemons = []

    def tearDown(self):
        for daemon in self.daemons:
            if daemon.poll() is None:
                daemon.terminate()
            daemon.wait(timeout=20)
            for stream in (daemon.stdout, daemon.stderr):
                if stream is not None:
                    stream.close()
        self.provider.shutdown()
        self.provider.server_close()
        self.temp.cleanup()

    def daemon(self, *extra, env=None):
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                DAEMON,
                KEY_NAME,
                "serve",
                "--store",
                str(self.base / "store"),
                "--control",
                str(self.base / "control"),
                *extra,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**self.env, **(env or {})},
        )
        self.daemons.append(process)
        return process

    def cli(self, socket, *args):
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "memoryking",
                *args,
                "--socket",
                socket,
                "--credential",
                str(self.owner_file),
            ],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=60,
        )
        self.assertEqual(0, run.returncode, run.stdout + run.stderr)
        return run.stdout

    def test_the_audit_hook_sees_a_process_open_the_key(self):
        canary = self.daemon("--help", env={"MK_CANARY": str(self.key)})
        canary.communicate(timeout=30)
        self.assertEqual("opened\n", self.log.read_text())

    def test_the_daemon_never_opens_the_key_while_its_child_does_the_work_and_reports(self):
        daemon = self.daemon(
            "--egress-policy",
            str(self.policy),
            "--consolidate-every",
            "2s",
            "--consolidate-idle",
            "0s",
            "--consolidate-project",
            "p",
            "--consolidate-provider",
            "openai-chat",
            "--consolidate-model",
            "grok-4",
        )
        socket = json.loads(daemon.stdout.readline())["socket"]
        self.cli(
            socket,
            "capture",
            "--project",
            "p",
            "--text",
            "The release train leaves every Thursday afternoon.",
            "--event",
            "sv1",
        )
        status = json.loads(self.cli(socket, "status", "--json"))
        self.assertEqual(["p"], [item["project"] for item in status["consolidation"]])
        # A child ran the pass: the provider saw the key that only the child read.
        self.assertTrue(wait_for(lambda: self.provider.authorizations, 40))
        self.assertEqual("Bearer " + KEY_VALUE, self.provider.authorizations[0])
        # The request reaches the provider before the child has filed its drafts; wait for them.
        self.assertTrue(
            wait_for(
                lambda: (
                    "Drafted from The release train"
                    in self.cli(socket, "review", "list", "--project", "p")
                ),
                40,
            )
        )
        seen = {}

        def reported():
            item = json.loads(self.cli(socket, "status", "--json"))["consolidation"][0]
            seen.update(item)
            return item["runs"] >= 1

        self.assertTrue(wait_for(reported, 20))
        self.assertIn(seen["state"], {"waiting", "running"})
        self.assertIn("Consolidation p:", self.cli(socket, "status"))
        pid = wait_for(
            lambda: json.loads(self.cli(socket, "status", "--json"))["consolidation"][0]["pid"],
            20,
        )
        daemon.send_signal(signal.SIGTERM)
        daemon.wait(timeout=30)
        if pid:
            self.assertTrue(wait_for(lambda: not alive(pid), 10))
        self.assertFalse(self.log.exists() and self.log.read_text(), "the daemon opened the key")

    def test_an_unknown_provider_stops_the_daemon_at_start_without_reading_the_key(self):
        daemon = self.daemon(
            "--egress-policy",
            str(self.policy),
            "--consolidate-every",
            "1h",
            "--consolidate-project",
            "p",
            "--consolidate-provider",
            "nowhere",
            "--consolidate-model",
            "x",
        )
        _, error = daemon.communicate(timeout=30)
        self.assertEqual(2, daemon.returncode, error)
        self.assertIn("egress_provider_denied", error)
        self.assertFalse(self.log.exists() and self.log.read_text())


if __name__ == "__main__":
    unittest.main()
