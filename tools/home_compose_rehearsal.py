"""Build and exercise disposable home profiles on local Docker; no accounts or mail.

The only provider override is in a generated test launcher outside the image.
Production source has no authentication bypass. Synthetic volumes are removed
after evidence collection unless --keep is explicitly selected for diagnosis.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import tarfile
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backend", choices=("local", "shared", "both"), default="both")
    parser.add_argument("--keep", action="store_true", help="Stop but retain synthetic volumes")
    parser.add_argument(
        "--compatible-revision",
        help="Optional prior full Git SHA for an actual packaged-core compatibility check",
    )
    args = parser.parse_args()
    if args.compatible_revision and (
        len(args.compatible_revision) != 40
        or any(c not in "0123456789abcdef" for c in args.compatible_revision)
    ):
        parser.error("--compatible-revision must be a full lowercase commit SHA")
    output = args.output.resolve()
    if output.exists():
        parser.error("Choose a new output directory; earlier evidence must be retained")
    output.mkdir(mode=0o700, parents=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT))
    suffix = uuid.uuid4().hex[:10]
    tag = revision[:12] + "-" + suffix
    environment = {
        k: v
        for k, v in os.environ.items()
        if k not in {"GMAIL_FROM", "GMAIL_APP_KEY", "KINDLE_TO", "TS_AUTHKEY", "TS_AUTH_KEY"}
    }
    environment["MK_HOME_TAG"] = tag
    result = {
        "revision": revision,
        "working_tree_changes": dirty,
        "host": platform.platform(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "image_tag": tag,
        "real_provider": False,
        "tailnet_enrolled": False,
        "mail_configured": False,
        "passed": False,
        "profiles": [],
        "commands": [],
    }
    counter = 0

    def run(command, *, input=None, allowed=(0,), timeout=300):
        nonlocal counter
        counter += 1
        started = time.monotonic()
        done = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        log = f"{counter:03d}.log"
        (output / log).write_text(done.stdout + done.stderr)
        result["commands"].append(
            {
                "command": command,
                "exit": done.returncode,
                "seconds": time.monotonic() - started,
                "log": log,
            }
        )
        (output / "result.json").write_text(json.dumps(result, indent=2))
        if done.returncode not in allowed:
            raise RuntimeError(f"Rehearsal command {counter} failed; inspect {output / log}")
        return done.stdout.strip()

    probe = (ROOT / "tools/home_container_probe.py").read_text()
    result["probe_sha256"] = hashlib.sha256(probe.encode()).hexdigest()
    result["docker"] = json.loads(run(["docker", "version", "--format", "{{json .}} "]))
    result["compose"] = run(["docker", "compose", "version", "--short"])
    context = run(["docker", "context", "show"])
    context_info = json.loads(run(["docker", "context", "inspect", context]))[0]
    endpoint = environment.get("DOCKER_HOST") or context_info["Endpoints"]["docker"]["Host"]
    if not endpoint.startswith("unix://"):
        raise RuntimeError("This local rehearsal requires a local Unix-socket Docker endpoint")
    result["docker_endpoint"] = endpoint
    overlay = output / "controlled-provider.json"
    overlay.write_text(
        json.dumps(
            {
                "services": {
                    "app": {
                        "entrypoint": [
                            "python",
                            "-c",
                            "from unittest.mock import patch\nfrom memoryking.home import main\n"
                            "with patch('memoryking.adapters.github_oauth.GitHubProvider.authenticate',return_value='12345'):\n"
                            " raise SystemExit(main())\n",
                        ]
                    }
                }
            }
        )
    )
    profiles = ("local", "shared") if args.backend == "both" else (args.backend,)
    baseline_image = None
    try:
        if args.compatible_revision:
            baseline_image = (
                "memoryking-home-compatibility:" + args.compatible_revision[:12] + "-" + suffix
            )
            with tempfile.TemporaryDirectory(prefix="mk-home-baseline-") as directory:
                archive = Path(directory) / "source.tar"
                with archive.open("wb") as stream:
                    subprocess.run(
                        ["git", "archive", "--format=tar", args.compatible_revision],
                        cwd=ROOT,
                        stdout=stream,
                        check=True,
                    )
                checkout = Path(directory) / "checkout"
                checkout.mkdir()
                with tarfile.open(archive) as source:
                    source.extractall(checkout, filter="data")
                run(
                    [
                        "docker",
                        "build",
                        "-f",
                        str(checkout / "deploy/shared/Dockerfile"),
                        "-t",
                        baseline_image,
                        str(checkout),
                    ],
                    timeout=600,
                )
            result["compatible_revision"] = args.compatible_revision
            result["compatible_image"] = json.loads(
                run(["docker", "image", "inspect", baseline_image])
            )[0]
        for backend in profiles:
            project = "mk-home-test-" + suffix + "-" + backend
            compose = [
                "docker",
                "compose",
                "-p",
                project,
                "-f",
                str(ROOT / "deploy/home/compose.yaml"),
            ]
            if backend == "shared":
                compose += ["-f", str(ROOT / "deploy/home/postgres.yaml")]
            configured = compose + ["-f", str(overlay)]
            item = {"backend": backend, "project": project, "passed": False}
            result["profiles"].append(item)
            try:
                run(
                    compose
                    + ["build", "app", "gateway"]
                    + (["postgres"] if backend == "shared" else []),
                    timeout=600,
                )
                run(compose + ["run", "--rm", "--no-deps", "bootstrap"])
                if backend == "shared":
                    run(compose + ["up", "-d", "--no-build", "--wait", "postgres"])
                    run(compose + ["run", "--rm", "--no-deps", "bootstrap", "postgres-init"])
                run(compose + ["up", "-d", "--no-build", "--wait", "gateway", "app"])
                app = run(compose + ["ps", "-q", "app"])
                gateway = run(compose + ["ps", "-q", "gateway"])
                # Docker running is not application readiness: verify the emitted marker.
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if '"ready": true' in run(["docker", "logs", app]):
                        break
                    time.sleep(0.2)
                else:
                    raise RuntimeError("Home process did not become ready")
                containers = [app, gateway]
                if backend == "shared":
                    containers.append(run(compose + ["ps", "-q", "postgres"]))
                inspections = json.loads(run(["docker", "inspect", *containers]))
                for inspected in inspections:
                    settings = inspected["HostConfig"]
                    assert not settings["Privileged"] and settings["ReadonlyRootfs"]
                    assert settings["CapDrop"] == ["ALL"] and not settings["CapAdd"]
                    assert not settings["PortBindings"] and not settings["Devices"]
                    assert "no-new-privileges:true" in settings["SecurityOpt"]
                    assert inspected["Config"]["User"] in {"10001:10001", "10002:10002"}
                    assert settings["Memory"] and settings["PidsLimit"]
                assert inspections[0]["HostConfig"]["NetworkMode"] == "container:" + gateway
                assert {m["Destination"] for m in inspections[1]["Mounts"]} == {
                    "/var/lib/tailscale"
                }
                assert all(m["Type"] == "volume" for c in inspections for m in c["Mounts"])
                item["containers"] = inspections
                item["images"] = json.loads(
                    run(["docker", "image", "inspect", *sorted({c["Image"] for c in inspections})])
                )
                status = json.loads(
                    run(
                        [
                            "docker",
                            "exec",
                            gateway,
                            "/usr/local/bin/tailscale",
                            "--socket=/run/tailscale/tailscaled.sock",
                            "status",
                            "--json",
                        ]
                    )
                )
                if backend == "local":
                    run(
                        [
                            "docker",
                            "run",
                            "--rm",
                            "--network",
                            "none",
                            "--user",
                            "10001:10001",
                            "--read-only",
                            "--cap-drop",
                            "ALL",
                            "--security-opt",
                            "no-new-privileges",
                            "--pids-limit",
                            "96",
                            "--memory",
                            "1g",
                            "--cpus",
                            "2",
                            "--tmpfs",
                            "/tmp:uid=10001,gid=10001,mode=0700,size=536870912",
                            "-e",
                            "MEMORYKING_REQUIRE_REPORT_SANDBOX=1",
                            "--mount",
                            f"type=bind,src={ROOT / 'tests'},dst=/tests,readonly",
                            "--entrypoint",
                            "python",
                            inspections[0]["Image"],
                            "-m",
                            "unittest",
                            "discover",
                            "-s",
                            "/tests",
                            "-p",
                            "test_report_sandbox.py",
                            "-v",
                        ]
                    )
                    item["linux_sandbox_suite"] = {
                        "passed": True,
                        "log": result["commands"][-1]["log"],
                    }
                assert status["BackendState"] == "NeedsLogin" and not status["TUN"]
                assert not status["CurrentTailnet"] and not status["TailscaleIPs"]
                item["sidecar"] = {
                    k: status[k] for k in ("Version", "TUN", "BackendState", "CurrentTailnet")
                }
                # Normal other UID cannot read configuration/control or connect to local IPC.
                denial = """import json,socket
from pathlib import Path
result={}
for name in ['/secrets/viewer.json','/control/security','/data/store/blobs']:
 try: p=Path(name);p.read_bytes() if p.is_file() else list(p.iterdir());result[name]=False
 except PermissionError:result[name]=True
print(json.dumps(result));assert all(result.values())
"""
                item["nonowner_denied"] = json.loads(
                    run(
                        ["docker", "exec", "-i", "--user", "10003:10003", app, "python", "-"],
                        input=denial,
                    )
                )
                run(compose + ["stop", "app"])
                stopped = json.loads(run(["docker", "inspect", app]))[0]
                assert not stopped["State"]["Running"] and stopped["State"]["ExitCode"] == 0
                item["disabled_start_and_orderly_exit"] = True
                item["prepare"] = json.loads(
                    run(
                        compose
                        + [
                            "run",
                            "--rm",
                            "--no-deps",
                            "-T",
                            "-e",
                            "MEMORYKING_HOME_REHEARSAL=1",
                            "--entrypoint",
                            "python",
                            "operator",
                            "-",
                            backend,
                            "prepare",
                        ],
                        input=probe,
                    )
                )
                run(configured + ["up", "-d", "--no-build", "app"])
                app = run(configured + ["ps", "-q", "app"])
                containers[0] = app
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if '"ready": true' in run(["docker", "logs", app]):
                        break
                    time.sleep(0.2)
                else:
                    raise RuntimeError("Configured home process did not become ready")
                for action in ("sandbox", "live", "network", "gateway"):
                    item[action] = json.loads(
                        run(
                            [
                                "docker",
                                "exec",
                                "-i",
                                "-e",
                                "MEMORYKING_HOME_REHEARSAL=1",
                                app,
                                "python",
                                "-",
                                backend,
                                action,
                            ],
                            input=probe,
                        )
                    )
                namespace = run(["docker", "exec", gateway, "readlink", "/proc/self/ns/net"])
                assert namespace == item["network"]["namespace"]
                address = next(iter(inspections[1]["NetworkSettings"]["Networks"].values()))[
                    "IPAddress"
                ]
                run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--network",
                        project + "_default",
                        "--read-only",
                        "--cap-drop",
                        "ALL",
                        "--security-opt",
                        "no-new-privileges",
                        "--entrypoint",
                        "python",
                        inspections[0]["Image"],
                        "-c",
                        "import socket,sys;s=socket.socket();s.settimeout(2);assert s.connect_ex((sys.argv[1],8772))!=0;print('unsupported network interface denied')",
                        address,
                    ]
                )
                item["unsupported_interface_denied"] = True
                item["stats"] = run(
                    ["docker", "stats", "--no-stream", "--format", "{{json .}}", *containers]
                )
                run(configured + ["stop", "app"])
                stopped = json.loads(run(["docker", "inspect", app]))[0]
                assert not stopped["State"]["Running"] and stopped["State"]["ExitCode"] == 0
                operator = compose + [
                    "run",
                    "--rm",
                    "--no-deps",
                    "-T",
                    "-e",
                    "MEMORYKING_HOME_REHEARSAL=1",
                    "--entrypoint",
                    "python",
                    "operator",
                    "-",
                    backend,
                ]
                item["direct_tls_configuration"] = json.loads(
                    run(operator + ["enable_tls"], input=probe)
                )
                run(configured + ["up", "-d", "--no-build", "app"])
                app = run(configured + ["ps", "-q", "app"])
                started = json.loads(run(["docker", "inspect", app]))[0]["State"]["StartedAt"]
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if '"ready": true' in run(["docker", "logs", "--since", started, app]):
                        break
                    time.sleep(0.2)
                else:
                    raise RuntimeError("Direct TLS home process did not become ready")
                item["direct_tls"] = json.loads(
                    run(
                        [
                            "docker",
                            "exec",
                            "-i",
                            "-e",
                            "MEMORYKING_HOME_REHEARSAL=1",
                            app,
                            "python",
                            "-",
                            backend,
                            "surface",
                        ],
                        input=probe,
                    )
                )
                run(configured + ["stop", "app"])
                stopped = json.loads(run(["docker", "inspect", app]))[0]
                assert not stopped["State"]["Running"] and stopped["State"]["ExitCode"] == 0
                if baseline_image:
                    compatibility = output / "compatible-core.json"
                    compatibility.write_text(
                        json.dumps({"services": {"operator": {"image": baseline_image}}})
                    )
                    read_core = """import hashlib,json,sys
from contextlib import ExitStack
from pathlib import Path
from memoryking.adapters.local_files import private_file
from memoryking.adapters.local_store import LocalStore
root,control=Path('/data/store'),Path('/control/security')
with ExitStack() as cleanup:
 if sys.argv[1]=='shared':
  from memoryking.adapters.postgres_pool import create_pool
  from memoryking.adapters.postgres_store import PostgresStore
  path=Path('/secrets/runtime.dsn');private_file(path)
  pool=create_pool(path.read_text(),size=2);cleanup.callback(pool.close)
  owner=(control/'main/owner.credential').read_text()
  store=cleanup.enter_context(PostgresStore(pool,root,control,'main',owner))
 else:
  owner=(control/'owner.credential').read_text()
  store=cleanup.enter_context(LocalStore(root,control))
 store.status(owner,'home')
 with store.transaction():
  value={'incarnation':store.incarnation,'store':store.store_id}
  for table in ('observations','events','report_revisions','report_files'):
   value[table]=store.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
 print(json.dumps({'canonical_sha256':hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()}))
"""
                    core_args = [
                        "run",
                        "--rm",
                        "--no-deps",
                        "-T",
                        "--entrypoint",
                        "python",
                        "operator",
                        "-",
                        backend,
                    ]
                    fingerprints = [
                        json.loads(run(compose + core_args, input=read_core)),
                        json.loads(
                            run(compose + ["-f", str(compatibility)] + core_args, input=read_core)
                        ),
                        json.loads(run(compose + core_args, input=read_core)),
                    ]
                    assert fingerprints[0] == fingerprints[1] == fingerprints[2]
                    item["compatible_core"] = {
                        "passed": True,
                        "prior_revision": args.compatible_revision,
                        "canonical_sha256": fingerprints[0]["canonical_sha256"],
                        "scope": "Actual prior/current packaged core opens the same stopped store; no schema migration or prior home entrypoint is claimed",
                    }
                item["startup_faults_denied"] = []
                for fault, repair in (
                    ("identity_hide", "identity_repair"),
                    ("tls_break", "tls_repair"),
                    ("schema_break", "schema_repair"),
                ):
                    run(operator + [fault], input=probe)
                    try:
                        failed = run(
                            compose + ["run", "--rm", "--no-deps", "-T", "app"], allowed=(1,)
                        )
                        assert '"ready": true' not in failed
                        item["startup_faults_denied"].append(fault)
                        if fault == "schema_break" and backend == "shared":
                            run(
                                compose
                                + [
                                    "run",
                                    "--rm",
                                    "--no-deps",
                                    "-T",
                                    "operator",
                                    "-m",
                                    "memoryking.shared",
                                    "migrate",
                                    "--dsn-file",
                                    "/pgadmin/migration.dsn",
                                ],
                                allowed=(2,),
                            )
                            assert (
                                "schema_unsupported" in (output / f"{counter:03d}.log").read_text()
                            )
                            item["unsupported_migration_denied"] = True
                    finally:
                        run(operator + [repair], input=probe)
                if backend == "shared":
                    run(compose + ["stop", "postgres"])
                    try:
                        started_failure = time.monotonic()
                        unavailable = run(
                            compose + ["run", "--rm", "--no-deps", "-T", "app"],
                            allowed=(1,),
                            timeout=30,
                        )
                        assert '"ready": true' not in unavailable
                        item["unavailable_database_start_seconds"] = (
                            time.monotonic() - started_failure
                        )
                    finally:
                        run(compose + ["up", "-d", "--no-build", "--wait", "postgres"])
                item["backup"] = json.loads(run(operator + ["backup"], input=probe))
                run(operator + ["restore_cut"], input=probe, allowed=(73,))
                failed_start = run(
                    compose + ["run", "--rm", "--no-deps", "-T", "app"], allowed=(1,)
                )
                assert '"ready": true' not in failed_start
                item["interrupted_restore_blocks_startup"] = True
                item["restore"] = json.loads(run(operator + ["restore"], input=probe))
                run(configured + ["up", "-d", "--no-build", "app"])
                app = run(configured + ["ps", "-q", "app"])
                started = json.loads(run(["docker", "inspect", app]))[0]["State"]["StartedAt"]
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if '"ready": true' in run(["docker", "logs", "--since", started, app]):
                        break
                    time.sleep(0.2)
                else:
                    raise RuntimeError("Restored home process did not become ready")
                item["restored"] = json.loads(
                    run(
                        [
                            "docker",
                            "exec",
                            "-i",
                            "-e",
                            "MEMORYKING_HOME_REHEARSAL=1",
                            app,
                            "python",
                            "-",
                            backend,
                            "restored",
                        ],
                        input=probe,
                    )
                )
                # Recreate the complete network namespace, preserving named volumes.
                # Restarting only a gateway can strand a container in its old namespace.
                run(configured + ["down"])
                run(configured + ["up", "-d", "--no-build", "--wait", "gateway", "app"])
                app = run(configured + ["ps", "-q", "app"])
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if '"ready": true' in run(["docker", "logs", app]):
                        break
                    time.sleep(0.2)
                else:
                    raise RuntimeError("Persistent-volume restart did not become ready")
                item["persistent_restart"] = json.loads(
                    run(
                        [
                            "docker",
                            "exec",
                            "-i",
                            "-e",
                            "MEMORYKING_HOME_REHEARSAL=1",
                            app,
                            "python",
                            "-",
                            backend,
                            "restored",
                        ],
                        input=probe,
                    )
                )
                item["passed"] = True
            finally:
                run(configured + ["down"] + ([] if args.keep else ["--volumes"]), timeout=300)
        result["passed"] = all(profile["passed"] for profile in result["profiles"])
    finally:
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        (output / "result.json").write_text(json.dumps(result, indent=2))
    print(
        json.dumps(
            {
                "passed": result["passed"],
                "profiles": len(profiles),
                "evidence": str(output),
                "immutable_checkout": not dirty,
            }
        )
    )


if __name__ == "__main__":
    main()
