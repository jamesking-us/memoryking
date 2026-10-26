"""Run the public viewer shell in a disposable local instance; no login or external network."""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


def get(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", path, headers={"Host": "viewer.example.invalid"})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def rehearse(command, config, config_file, ready, *, exit_codes=(0,)):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"PYTHONPATH", "PYTHONHOME", "GMAIL_FROM", "GMAIL_APP_KEY", "KINDLE_TO"}
    }
    secret = config_file.with_suffix(".secret")
    secret.write_text("synthetic-local-fixture-client-secret")
    secret.chmod(0o600)
    config["github_secret_file"] = str(secret)
    results = []
    for mode in ("omitted", "disabled", "enabled"):
        config["enabled"] = mode == "enabled"
        config_file.write_text(json.dumps(config))
        config_file.chmod(0o600)
        args = command + ([] if mode == "omitted" else ["--viewer-config", str(config_file)])
        with tempfile.TemporaryFile(mode="w+") as log:
            process = subprocess.Popen(args, env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 10
                while not ready():
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("Viewer CLI did not start")
                    time.sleep(0.05)
                if mode == "enabled":
                    while True:
                        try:
                            assert get(config["listen_port"], "/viewer/")[0] == 200
                            break
                        except OSError:
                            if time.monotonic() > deadline:
                                raise
                            time.sleep(0.02)
                    assert get(config["listen_port"], "/viewer/api/v1/session")[0] == 401
                    assert get(config["listen_port"], "/v1/read")[0] == 404
                else:
                    try:
                        get(config["listen_port"], "/viewer/")
                    except OSError:
                        pass
                    else:
                        raise RuntimeError("Disabled viewer unexpectedly bound a socket")
                results.append({"mode": mode, "passed": True})
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise RuntimeError("Viewer CLI did not stop cleanly") from None
            assert process.returncode in exit_codes, process.returncode
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", config["listen_port"]))
    return results


def main():
    with tempfile.TemporaryDirectory(prefix="mk-viewer-demo-") as temporary:
        root = Path(temporary)
        store, control = root / "data", root / "control"
        common = ["--store", str(store), "--control", str(control)]
        subprocess.run(
            [sys.executable, "-m", "memoryking", "init", *common], check=True, capture_output=True
        )
        config = json.loads((ROOT / "examples/viewer-config.json").read_text())
        config["listen_port"] = free_port()
        results = rehearse(
            [sys.executable, "-m", "memoryking", "serve", *common],
            config,
            root / "viewer.json",
            lambda: (store / "service.sock").exists(),
        )
        print(
            json.dumps(
                {
                    "passed": True,
                    "viewer_shell_only": True,
                    "production_login": False,
                    "results": results,
                }
            )
        )


if __name__ == "__main__":
    main()
