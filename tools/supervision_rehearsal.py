"""Exercise a temporary macOS launchd supervisor and remove it on exit."""

import argparse
import json
import os
import platform
import plistlib
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from memoryking.adapters.local_files import atomic
from memoryking.adapters.local_store import LocalStore
from memoryking.domain.local import LocalError
from memoryking.sdk import LocalClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise SystemExit(
            "This rehearsal is macOS launchd only; other supervisors remain unverified"
        )
    output = args.output.resolve()
    if output.exists():
        raise SystemExit("Use a new evidence output directory")
    output.mkdir(parents=True)
    report = {
        "source_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[1], text=True
        ).strip(),
        "platform": platform.platform(),
        "scope": "Temporary user launchd service; no persistent service remains",
        "passed": False,
    }
    label = "com.memoryking.rehearsal." + uuid.uuid4().hex
    target = f"gui/{os.getuid()}/{label}"
    loaded = False
    with tempfile.TemporaryDirectory(prefix="mk-supervision-", dir="/tmp") as temp:
        root = Path(temp)
        data = root / "data"
        control = root / "control"
        credential = LocalStore.initialize(data, control)
        service = {
            "Label": label,
            "ProgramArguments": [
                sys.executable,
                "-I",
                "-m",
                "memoryking",
                "serve",
                "--store",
                str(data),
                "--control",
                str(control),
            ],
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 1,
            "Umask": 63,
            "StandardOutPath": str(root / "stdout.log"),
            "StandardErrorPath": str(root / "stderr.log"),
        }
        plist = root / "service.plist"
        atomic(plist, plistlib.dumps(service))

        def run(*args):
            return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=15)

        def wait_client():
            deadline = time.monotonic() + 30
            while True:
                try:
                    return LocalClient(data / "service.sock", credential, "demo")
                except LocalError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Supervised service did not become ready") from None
                    time.sleep(0.2)

        try:
            start = run("bootstrap", f"gui/{os.getuid()}", str(plist))
            report["bootstrap_exit_code"] = start.returncode
            if start.returncode:
                raise RuntimeError("launchd bootstrap rejected the temporary fixture")
            loaded = True
            first = wait_client()
            observation = first.capture(
                "supervised",
                {
                    "version": 1,
                    "project": "demo",
                    "source": "supervision",
                    "source_event": "one",
                    "text": "Synthetic acknowledged process-crash record",
                    "readers": None,
                    "classification": "internal",
                    "observed_at_ms": 0,
                },
            )
            state = run("print", target)
            match = re.search(r"\bpid = (\d+)", state.stdout)
            if state.returncode or not match:
                raise RuntimeError("Could not identify the owned supervised process")
            pid = int(match.group(1))
            os.kill(pid, signal.SIGKILL)
            started = time.monotonic()
            deadline = started + 30
            while True:
                state = run("print", target)
                match = re.search(r"\bpid = (\d+)", state.stdout)
                if match and int(match.group(1)) != pid:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("launchd did not restart the killed service")
                time.sleep(0.2)
            recovered = wait_client()
            context = recovered.context()
            assert context["incarnation"] == observation["incarnation"]
            contents = recovered._request(
                recovered._credential(),
                "read",
                {"project": "demo", "observation": observation["handle"]},
            )
            assert contents["items"][0]["id"] == observation["observation"]
            report.update(
                passed=True,
                supervisor_restarted=True,
                acknowledged_capture_survived=True,
                ordinary_crash_preserves_history=True,
                recovery_ms=round((time.monotonic() - started) * 1000),
            )
        finally:
            if loaded:
                stop = run("bootout", target)
                report["bootout_exit_code"] = stop.returncode
                if stop.returncode:
                    report["passed"] = False
            for name in ["stdout.log", "stderr.log"]:
                if (root / name).exists():
                    (output / name).write_bytes((root / name).read_bytes())
            (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    if not report["passed"]:
        raise SystemExit(1)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
