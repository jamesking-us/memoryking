#!/usr/bin/env python3
"""Native disposable shared-service pilot. Synthetic data; no model or remote calls.

This measures one host/configuration, not a release envelope or a quality study.
Each arm starts with fresh tenants and runs the same seeded 90%-hot request mix.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import hashlib
import json
import math
import os
import platform
import random
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import psycopg
import uvicorn
from postgres_dev_cluster import DevCluster

from memoryking.adapters.inference import Inference
from memoryking.adapters.knowledge_store import Knowledge
from memoryking.adapters.postgres_admin import initialize_tenant, migrate, provision_roles
from memoryking.adapters.postgres_pool import create_pool
from memoryking.adapters.retrieval import Retrieval
from memoryking.adapters.shared_http import create_app
from memoryking.adapters.shared_service import SharedService
from memoryking.adapters.working import Working
from memoryking.domain.local import LocalError, Released
from memoryking.harness_context import AuthorizedConversation, LocalRecordingProvider

ROOT = Path(__file__).resolve().parents[1]


def unpack(result):
    if isinstance(result, Released):
        output = []
        result.emit(output.append)
        return json.loads(output[0])
    return result


def percentiles(values):
    values = sorted(values)
    return {
        "n": len(values),
        **{
            f"p{p}": round(values[max(0, math.ceil(len(values) * p / 100) - 1)], 3)
            if values
            else None
            for p in (50, 95, 99)
        },
        "max": round(max(values), 3) if values else None,
    }


class MeasuredService(SharedService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.measurements = []
        self.measure_lock = threading.Lock()
        self.local = threading.local()

    @contextmanager
    def store(self, tenant, credential, *, release_only=False):
        with super().store(tenant, credential, release_only=release_only) as store:
            try:
                yield store
            finally:
                with self.measure_lock:
                    self.measurements.extend(
                        {
                            "tenant": tenant,
                            "operation": getattr(self.local, "operation", "dashboard"),
                            **row,
                        }
                        for row in store.metrics
                    )


class DirectClient:
    """Pilot transport adapter; invokes the real service, never replaces authorization."""

    def __init__(self, service, tenant, credential):
        self.service, self.tenant, self.credential = service, tenant, credential

    def request(self, operation, args):
        return unpack(
            self.service.execute(
                self.tenant, self.credential, {"version": 1, "operation": operation, "args": args}
            )
        )

    def context(self):
        return self.request("context", {"project": "p"})

    def inference_status(self, command):
        return self.request("inference", {"command": command})


def run_arm(cluster, base, fanout, repetitions):
    root, control = base / "content", base / "control"
    credentials, namespaces, workers, seeds = {}, {}, {}, {}
    pool = create_pool(cluster.dsn("mk_runtime"), size=8)
    worker_pool = create_pool(cluster.dsn("mk_worker"), size=2)
    service = MeasuredService(pool, root, control)
    worker_service = MeasuredService(worker_pool, root, control)
    tenants = [f"arm{fanout}_t{i}" for i in range(5)]
    checkpoint = json.loads((ROOT / "fixtures/contracts/checkpoint.json").read_text())
    checkpoint["objective"] = "Synthetic contention fixture; no production work"
    provider = LocalRecordingProvider(base / "recording")
    conversations = {}
    for tenant in tenants:
        with psycopg.connect(cluster.dsn("mk_migration"), autocommit=True) as migration:
            credentials[tenant] = initialize_tenant(migration, root, control, tenant).read_text()
        credential = credentials[tenant]
        with service.store(tenant, credential) as store:
            namespaces[tenant] = store.namespace(credential, "p")
            workers[tenant] = store.grant(credential, "worker", "p", ["read", "jobs"])
            seeds[tenant] = store.capture(
                credential, namespaces[tenant], "seed", observation("seed")
            )
        conversations[tenant] = AuthorizedConversation(
            DirectClient(service, tenant, credential), provider
        )
    # Create an accepted mandatory constraint; zero text similarity cannot omit it.
    hot = tenants[0]
    with service.store(hot, credentials[hot]) as store:
        body = {
            "text": "Never transmit fixture data to a remote provider",
            "kind": "constraint",
            "selector": {"selector_version": "1", "any_of": [{"all_of": [{"op": "any"}]}]},
            "valid_start": None,
            "valid_end": None,
            "task": None,
            "run": None,
            "readers": None,
            "evidence": [],
            "authority": "project",
            "rule_key": "egress",
            "effect": "require",
            "rule_value": "local-only",
            "provenance": {},
        }
        # Constraints may require evidence; capture a reviewed source and cite exact bytes.

        text = body["text"]
        seed = store.capture(
            credentials[hot], namespaces[hot], "constraint", observation("constraint", text)
        )
        body["evidence"] = [
            {
                "observation": seed["observation"],
                "start_byte": 0,
                "end_byte": len(text.encode()),
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            }
        ]
        proposed = unpack(
            Knowledge(store).execute(
                credentials[hot],
                {
                    "version": 1,
                    "incarnation": store.incarnation,
                    "action": "propose",
                    "key": "constraint",
                    "args": {
                        "project": "p",
                        "logical_key": "egress",
                        "expected_version": 0,
                        "body": body,
                    },
                },
            )
        )
        unpack(
            Knowledge(store).execute(
                credentials[hot],
                {
                    "version": 1,
                    "incarnation": store.incarnation,
                    "action": "review",
                    "key": "review",
                    "args": {
                        "project": "p",
                        "revision": proposed["revision"],
                        "expected_version": proposed["family_version"],
                        "decision": "accept",
                        "reason": "synthetic_review",
                        "breadth_reviewed": True,
                    },
                },
            )
        )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(32)
    port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    app = create_app(
        service,
        hosts=[f"127.0.0.1:{port}"],
        origins=[url],
        loopback_http=True,
        plan_root=ROOT,
        dashboard_tenant=hot,
    )
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    server_thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
    server_thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        if time.monotonic() >= deadline:
            raise RuntimeError("pilot HTTP startup failed")
        time.sleep(0.01)
    stop = threading.Event()
    stream_counts = Counter()
    stream_lock = threading.Lock()

    def stream():
        while not stop.is_set():
            connected = False
            session = None
            try:
                bootstrap = service.dashboard.issue(hot, credentials[hot])
                session = service.dashboard.exchange(bootstrap["code"], "")
                req = urllib.request.Request(
                    url + "/api/events", headers={"Cookie": "mk_session=" + session}
                )
                with urllib.request.urlopen(req, timeout=4) as response:
                    with stream_lock:
                        stream_counts["connections"] += 1
                        connected = True
                        stream_counts["active"] += 1
                        stream_counts["peak_active"] = max(
                            stream_counts["peak_active"], stream_counts["active"]
                        )
                    while not stop.is_set():
                        line = response.readline()
                        if not line:
                            break
                        with stream_lock:
                            stream_counts["bytes"] += len(line)
                            if line.startswith(b"event: "):
                                stream_counts[line.decode().strip()[7:]] += 1
                        if line.startswith(b"event: context-reset"):
                            break
            except Exception as exc:
                with stream_lock:
                    stream_counts[
                        "errors:"
                        + (
                            exc.code
                            if isinstance(exc, LocalError)
                            else "HTTP" + str(exc.code)
                            if isinstance(exc, urllib.error.HTTPError)
                            else type(exc).__name__
                        )
                    ] += 1
                stop.wait(0.05)
            finally:
                if connected:
                    with stream_lock:
                        stream_counts["active"] -= 1
                if session is not None:
                    with service.dashboard.lock:
                        service.dashboard.sessions.pop(session, None)

    threads = [threading.Thread(target=stream) for _ in range(fanout)]
    for thread in threads:
        thread.start()
    operations = [
        "capture",
        "capture",
        "checkpoint",
        "constraints",
        "inference",
        "reindex",
        "worker",
        "policy",
        "read",
        "brief",
    ]
    jobs = []
    for op_index, operation in enumerate(operations):
        for index in range(20 * repetitions):
            tenant = (
                hot if index % 20 < 18 else tenants[((index // 20) * 2 + index % 20 - 18) % 4 + 1]
            )
            jobs.append((tenant, operation, f"{op_index}_{index}"))
    random.Random(701).shuffle(jobs)
    before_positions = {}
    for tenant in tenants:
        with service.store(tenant, credentials[tenant]) as store:
            before_positions[tenant] = store.status(credentials[tenant], "p")["position"]
    rows = []

    def perform(job):
        tenant, operation, key = job
        credential = workers[tenant] if operation == "worker" else credentials[tenant]
        svc = worker_service if operation == "worker" else service
        svc.local.operation = operation
        started = time.monotonic()
        status = "passed"
        try:
            with svc.store(tenant, credential, release_only=operation == "read") as store:
                if operation == "capture":
                    store.capture(credential, namespaces[tenant], key, observation(key))
                elif operation == "checkpoint":
                    checkpoint_result = unpack(
                        Working(store).execute(
                            credential,
                            {
                                "version": 1,
                                "incarnation": store.incarnation,
                                "action": "save",
                                "key": key,
                                "args": {
                                    "project": "p",
                                    "task": "task_" + key,
                                    "run": None,
                                    "expected_revision": 0,
                                    "readers": ["owner"],
                                    "checkpoint": checkpoint,
                                },
                            },
                        )
                    )
                    if checkpoint_result.get("status") != "durable":
                        status = checkpoint_result.get("status", "unexpected_checkpoint_status")
                elif operation == "constraints":
                    result = unpack(
                        Knowledge(store).execute(
                            credential,
                            {
                                "version": 1,
                                "incarnation": store.incarnation,
                                "action": "constraints",
                                "args": {"project": "p", "bindings": [{}]},
                            },
                        )
                    )
                    if result["status"] == "blocked":
                        status = "policy_blocked"
                    elif tenant == hot and not result["items"]:
                        raise AssertionError("Mandatory constraint missing")
                elif operation == "inference":
                    context = unpack(store.context(credential, "p"))
                    binding = {
                        k: context[k]
                        for k in (
                            "store",
                            "tenant",
                            "principal",
                            "project",
                            "incarnation",
                            "authorization_epoch",
                            "purge_epoch",
                            "delegation_chain",
                        )
                    }
                    binding.update(
                        egress_policy_revision="local-recording-v1",
                        model_session_id="pilot_" + key,
                        context_epoch=0,
                    )
                    args = json.loads(
                        (ROOT / "fixtures/contracts/inference-vectors.json").read_text()
                    )[0]["request"]["args"]
                    args["binding"] = binding
                    args["manifest"]["segments"][0]["host_authorizations"][0]["expires_ms"] = (
                        store.clock.utc_ms() + 60000
                    )
                    permit = unpack(
                        Inference(store).execute(
                            credential, {"version": 1, "action": "authorize", "args": args}
                        )
                    )
                    unpack(
                        Inference(store).execute(
                            credential,
                            {
                                "version": 1,
                                "action": "consume",
                                "args": {**args, "permit": permit["permit"]},
                            },
                        )
                    )
                elif operation == "reindex":
                    store.reindex(credential)
                elif operation == "worker":
                    lease = store.claim(credential, "p")
                    if lease:
                        store.finish(credential, "p", lease["lease"], "complete")
                elif operation == "policy":
                    store.grant(credential, "reader_" + key, "p", ["read"])
                    store.revoke(credential, "reader_" + key, "p")
                    if int(key.split("_")[1]) % 5 == 0:
                        changed = copy.deepcopy(body)
                        changed.update(
                            rule_key="temporary_" + key,
                            text="Synthetic temporary policy",
                            valid_end=store.clock.utc_ms() + 5000,
                        )
                        source = observation("seed")["text"].encode()
                        changed["evidence"] = [
                            {
                                "observation": seeds[tenant]["observation"],
                                "start_byte": 0,
                                "end_byte": len(source),
                                "content_hash": hashlib.sha256(source).hexdigest(),
                            }
                        ]
                        proposed = unpack(
                            Knowledge(store).execute(
                                credential,
                                {
                                    "version": 1,
                                    "incarnation": store.incarnation,
                                    "action": "propose",
                                    "key": "propose_" + key,
                                    "args": {
                                        "project": "p",
                                        "logical_key": "temporary_" + key,
                                        "expected_version": 0,
                                        "body": changed,
                                    },
                                },
                            )
                        )
                        for decision, version in (
                            ("accept", proposed["family_version"]),
                            ("quarantine", proposed["family_version"] + 1),
                            ("release_quarantine", proposed["family_version"] + 2),
                        ):
                            unpack(
                                Knowledge(store).execute(
                                    credential,
                                    {
                                        "version": 1,
                                        "incarnation": store.incarnation,
                                        "action": "review",
                                        "key": decision + key,
                                        "args": {
                                            "project": "p",
                                            "revision": proposed["revision"],
                                            "expected_version": version,
                                            "decision": decision,
                                            "reason": "synthetic_policy_churn",
                                            "breadth_reviewed": True,
                                        },
                                    },
                                )
                            )
                elif operation == "read":
                    unpack(store.read(credential, "p", query="synthetic"))
                elif operation == "brief":
                    result = unpack(
                        Retrieval(store).execute(
                            credential,
                            {
                                "version": 1,
                                "incarnation": store.incarnation,
                                "action": "brief",
                                "args": {
                                    "project": "p",
                                    "query": "synthetic",
                                    "bindings": [{}],
                                    "max_output_tokens": 6000,
                                    "tokenizer": "cl100k_base",
                                },
                            },
                        )
                    )
                    if result["status"] in {"blocked", "budget_insufficient", "incomplete"}:
                        status = result["status"]
                    elif tenant == hot and not result.get("constraints"):
                        raise AssertionError("Mandatory briefing constraint missing")
        except Exception as exc:
            status = (
                exc.code
                if isinstance(exc, LocalError)
                else type(exc).__name__ + ":" + str(exc)[:160]
            )
        return {
            "tenant": tenant,
            "operation": operation,
            "status": status,
            "total_ms": (time.monotonic() - started) * 1000,
        }

    service.local.operation = "context-monitor"
    ready_deadline = time.monotonic() + 30
    streams_ready = fanout == 0
    while not streams_ready and time.monotonic() < ready_deadline:
        with stream_lock:
            streams_ready = stream_counts["active"] == fanout
        if not streams_ready:
            time.sleep(0.05)
    began = time.monotonic()
    try:
        if not streams_ready:
            raise RuntimeError("All requested dashboard streams must connect before load")
        with service.measure_lock:
            service.measurements.clear()
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            futures = [executor.submit(perform, job) for job in jobs]
            while any(not f.done() for f in futures):
                for tenant, conversation in conversations.items():
                    try:
                        conversation._sync()
                    except LocalError:
                        pass
                time.sleep(0.05)
            rows = [future.result() for future in futures]
        duration = time.monotonic() - began
    finally:
        stop.set()
        for thread in threads:
            thread.join(6)
        server.should_exit = True
        server_thread.join(6)
        listener.close()
    metrics = service.measurements + worker_service.measurements
    with service.store(hot, credentials[hot]) as store:
        queue = store.status(credentials[hot], "p")["jobs"]
    event_counts = {}
    for tenant in tenants:
        with service.store(tenant, credentials[tenant]) as store:
            event_counts[tenant] = (
                store.status(credentials[tenant], "p")["position"] - before_positions[tenant]
            )
    pool_stats = pool.get_stats()
    pool.close()
    worker_pool.close()

    def group(field, values):
        return {
            value: {
                "requests": sum(r[field] == value for r in rows),
                "statuses": dict(Counter(r["status"] for r in rows if r[field] == value)),
                "total_ms": percentiles([r["total_ms"] for r in rows if r[field] == value]),
            }
            for value in values
        }

    return {
        "dashboard_fanout": fanout,
        "all_streams_connected_before_load": streams_ready,
        "requests": len(rows),
        "duration_s": round(duration, 3),
        "hot_request_fraction": sum(r["tenant"] == hot for r in rows) / len(rows),
        "hot_committed_mutation_fraction": sum(
            r["tenant"] == hot
            and r["status"] == "passed"
            and r["operation"] in {"capture", "checkpoint", "policy"}
            for r in rows
        )
        / max(
            1,
            sum(
                r["status"] == "passed" and r["operation"] in {"capture", "checkpoint", "policy"}
                for r in rows
            ),
        ),
        "domain_events_by_tenant": event_counts,
        "hot_domain_event_fraction": event_counts[hot] / max(1, sum(event_counts.values())),
        "statuses": dict(Counter(r["status"] for r in rows)),
        "by_operation": group("operation", sorted(set(operations))),
        "by_tenant": group("tenant", tenants),
        "timing_ms": {
            key: percentiles([r[key] for r in metrics if key in r])
            for key in ("gate_wait_ms", "clock_wait_ms", "transaction_ms", "anchor_ms")
        },
        "dashboard_streams": dict(stream_counts),
        "context_rebuilds": sum(c.context_epoch for c in conversations.values()),
        "pool": pool_stats,
        "hot_queue_states": queue,
        "shared_cache_hits": 0,
        "shared_cache_policy": "Fresh bounded request cache; no cross-request cache",
        "raw_requests": rows,
        "raw_transactions": metrics,
    }


def observation(key, text="synthetic observation for native contention"):
    return {
        "version": 1,
        "project": "p",
        "source": "pilot",
        "source_event": str(key),
        "text": text,
        "readers": None,
        "classification": "internal",
        "observed_at_ms": 0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 10 or args.output.exists():
        raise SystemExit("Use a new output path and repetitions 1..10")
    with DevCluster() as cluster, TemporaryDirectory(prefix="mk-pilot-", dir="/tmp") as directory:
        with psycopg.connect(cluster.dsn(), autocommit=True) as admin:
            provision_roles(admin)
        with psycopg.connect(cluster.dsn("mk_migration"), autocommit=True) as migration:
            migrate(migration)
        version = subprocess.check_output(
            [str(cluster.bin / "postgres"), "--version"], text=True
        ).strip()
        result = {
            "schema_version": 1,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "source_revision": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            "pilot_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "kind": "synthetic-contention-pilot",
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "postgresql": version,
            "python": platform.python_version(),
            "pool_size": 8,
            "worker_pool_size": 2,
            "concurrency": 16,
            "seed": 701,
            "provider_calls": 0,
            "external_network": False,
            "arms": [
                run_arm(cluster, Path(directory) / f"arm{fanout}", fanout, args.repetitions)
                for fanout in (0, 8)
            ],
            "qualification": "Measurements only; not production load or release qualification",
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    allowed = {
        "passed",
        "stale_lease",
        "inference_context_changed",
        "policy_blocked",
        "blocked",
        "budget_insufficient",
        "incomplete",
        "service_busy",  # Explicit pre-transaction admission pressure; still a failed request.
    }
    result["unexpected_statuses"] = sorted(
        {status for arm in result["arms"] for status in arm["statuses"] if status not in allowed}
    )
    if any(arm["hot_domain_event_fraction"] < 0.8 for arm in result["arms"]):
        result["unexpected_statuses"].append("insufficient_hot_tenant_concentration")
    # Keep the raw failed run before returning nonzero; a negative pilot is evidence.
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "arms"}))
    for arm in result["arms"]:
        print(
            json.dumps(
                {k: v for k, v in arm.items() if k not in {"raw_requests", "raw_transactions"}}
            )
        )

    if result["unexpected_statuses"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
