"""Synthetic five-viewer TLS workload on fresh native stores; no accounts or mail.

HTTP, authorization, release receipts and database operations are real. Only the
identity provider is controlled. This cannot qualify a phone or a tailnet path.
"""

from __future__ import annotations

import argparse
import copy
import http.client
import json
import math
import os
import platform
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]


def distribution(values):
    values = sorted(values)
    return {
        "n": len(values),
        **{
            f"p{p}": values[max(0, math.ceil(len(values) * p / 100) - 1)] if values else None
            for p in (50, 95, 99)
        },
        "max": max(values) if values else None,
    }


class Ledger:
    """Stream sanitized metrics; never retain response bodies, cookies or tokens."""

    def __init__(self, path):
        self.file = path.open("x")
        self.lock = threading.Lock()
        self.epoch = time.monotonic()

    def write(self, kind, **fields):
        row = {"kind": kind, "elapsed_s": time.monotonic() - self.epoch, **fields}
        with self.lock:
            self.file.write(json.dumps(row, separators=(",", ":")) + "\n")
            self.file.flush()

    def close(self):
        self.file.close()


class MeasuredLock:
    """Delegate the exact SQLite RLock; observe admission without changing locking."""

    def __init__(self, delegate, ledger):
        self.delegate, self.ledger = delegate, ledger

    def acquire(self, *args, **kwargs):
        started = time.monotonic()
        acquired = self.delegate.acquire(*args, **kwargs)
        self.ledger.write("sqlite_lock", wait_ms=(time.monotonic() - started) * 1000)
        return acquired

    def release(self):
        self.delegate.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()


class Client:
    def __init__(self, fixture, ledger, number):
        self.f, self.ledger, self.number = fixture, ledger, number
        self.cookies = {}

    def request(self, path, *, phase, method="GET", body=None):
        headers = {"Host": urlsplit(self.f.settings["public_origin"]).netloc}
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if method == "POST":
            headers.update(
                {"Content-Type": "application/json", "Origin": self.f.settings["public_origin"]}
            )
            body = json.dumps(body or {}).encode()
        connection = http.client.HTTPSConnection(
            "127.0.0.1", self.f.port, context=self.f.tls_context, timeout=8
        )
        started = time.monotonic()
        status, size, data, error = 0, 0, {}, None
        # Paths may contain opaque cursors/handles; retain the route category only.
        route = path.split("?")[0].rsplit("/", 1)[-1]
        if len(route) > 96:
            route = "memory_detail"
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            status = response.status
            raw = response.read(262145)
            size = len(raw)
            if len(raw) > 262144:
                raise AssertionError("response byte bound exceeded")
            for name, value in response.getheaders():
                if name.lower() == "set-cookie":
                    cookie = SimpleCookie(value)
                    for key, item in cookie.items():
                        if item["max-age"] == "0":
                            self.cookies.pop(key, None)
                        else:
                            self.cookies[key] = item.value
            data = json.loads(raw) if raw else {}
            if status == 200 and "/api/" in path:
                assert response.getheader("Cache-Control") == "no-store"
                assert data["binding"]["fresh_until_ms"] - data["binding"]["evaluated_ms"] <= 15000
                projected = data["data"]
                assert len(projected.get("tasks", [])) <= 50
                assert len(projected.get("memories", [])) <= 50
                assert len(projected.get("nodes", [])) <= 100
                assert len(projected.get("edges", [])) <= 150
                assert b"OWNER_ONLY_LOAD_MARKER" not in raw
        except Exception as exc:
            error = type(exc).__name__
        finally:
            connection.close()
            elapsed = (time.monotonic() - started) * 1000
            self.ledger.write(
                "request",
                viewer=self.number,
                phase=phase,
                route=route,
                status=status,
                ms=elapsed,
                body_bytes=size,
                error=error,
            )
        if error:
            raise RuntimeError(error)
        return status, data

    def login(self, phase):
        from memoryking.adapters.viewer_auth import CALLBACK

        # Only provider emulation is serialized: real reads remain concurrent.
        with self.f.login_lock:
            status, context = self.request("/viewer/auth/context", phase=phase)
            assert status == 200
            status, start = self.request(
                "/viewer/auth/github/start",
                phase=phase,
                method="POST",
                body={"csrf": context["csrf"]},
            )
            assert status == 200
            from urllib.parse import parse_qs

            query = parse_qs(urlsplit(start["authorization_url"]).query)
            self.f.provider.challenge = query["code_challenge"][0]
            self.f.provider.user_id = str(12345 + self.number)
            status, _ = self.request(
                CALLBACK
                + "?state="
                + query["state"][0]
                + "&code=controlled-code&iss=https%3A%2F%2Fgithub.com%2Flogin%2Foauth",
                phase=phase,
            )
            assert status == 303


def seed(fixture, tasks, memories):
    from memoryking.adapters.working import Working

    # The fixture already owns exactly one canonical observation. Add the rest
    # through capture, including one owner-only non-disclosure sentinel.
    for i in range(1, memories):
        fixture.store.capture(
            fixture.owner,
            fixture.ns,
            f"load-{i}",
            {
                "version": 1,
                "project": "p",
                "source": "synthetic-mobile-load",
                "source_event": f"load-{i}",
                "text": "OWNER_ONLY_LOAD_MARKER"
                if i == memories - 1
                else f"Synthetic memory {i}: retry evidence for task {i % tasks}. "
                + "Bounded data. " * 40,
                "readers": ["owner"] if i == memories - 1 else None,
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )
    body = json.loads((ROOT / "fixtures/contracts/checkpoint.json").read_text())
    body.update(findings=[], failed_attempts=[], verification=[], blockers=[], references=[])
    for i in range(tasks):
        checkpoint = copy.deepcopy(body)
        checkpoint.update(objective=f"Synthetic task {i}", current_state="Awaiting real validation")
        Working(fixture.store).execute(
            fixture.owner,
            {
                "version": 1,
                "incarnation": fixture.store.incarnation,
                "action": "save",
                "key": f"load-task-{i}",
                "args": {
                    "project": "p",
                    "task": f"task-{i:03}",
                    "run": None,
                    "expected_revision": 0,
                    "readers": ["owner", "phone", "phone1", "phone2", "phone3", "phone4"],
                    "checkpoint": checkpoint,
                },
            },
        )
    # Real inspection of seeded cardinality, no direct writes/counter fabrication.
    with fixture.store.transaction():
        observed = fixture.store.db.execute(
            "SELECT COUNT(*) FROM observations WHERE tenant=? AND project=? AND deleted=0",
            (fixture.tenant, "p"),
        ).fetchone()[0]
        saved_tasks = fixture.store.db.execute(
            "SELECT COUNT(*) FROM checkpoint_streams WHERE tenant=? AND project=? AND stream='task'",
            (fixture.tenant, "p"),
        ).fetchone()[0]
    assert observed == memories
    assert saved_tasks == tasks
    return {"tasks": saved_tasks, "memories": observed, "owner_only_memories": 1}


def sample_processes(ledger, stop, pg_root):
    while not stop.is_set():
        output = subprocess.check_output(["ps", "-axo", "pid=,ppid=,rss=,%cpu="], text=True)
        rows = [line.split() for line in output.splitlines()]
        wanted = {os.getpid()}
        if pg_root:
            wanted.add(pg_root)
        for _ in range(5):
            wanted |= {int(p) for p, parent, *_ in rows if int(parent) in wanted}
        selected = [r for r in rows if int(r[0]) in wanted]
        ledger.write(
            "resources",
            rss_bytes=sum(int(r[2]) * 1024 for r in selected),
            cpu_percent_ps=sum(float(r[3]) for r in selected),
            processes=len(selected),
            host_load_averages=list(os.getloadavg()),
        )
        stop.wait(5)


def run_backend(fixture, backend, args, output):
    from memoryking.adapters.viewer_auth import SESSION_COOKIE

    output.mkdir()
    ledger = Ledger(output / "measurements.jsonl")
    start = time.monotonic()
    try:
        corpus = seed(fixture, 5 if args.smoke else 100, 20 if args.smoke else 2000)
    except BaseException:
        ledger.close()
        raise
    ledger.write("seed", seconds=time.monotonic() - start, **corpus)
    fixture.login_lock = threading.Lock()
    if backend == "sqlite":
        fixture.store.lock = MeasuredLock(fixture.store.lock, ledger)
    else:
        # Capture the adapter's native transaction/gate/clock timings before each
        # per-request store closes; observe rather than replace database work.
        original_actor, original_auth = fixture.actor_store, fixture.auth_store

        def instrument(factory):
            @contextmanager
            def measured(*a, **kw):
                with factory(*a, **kw) as store:
                    try:
                        yield store
                    finally:
                        for metric in store.metrics:
                            ledger.write("postgres_transaction", **metric)
                        store.metrics.clear()

            return measured

        fixture.actor_store, fixture.auth_store = (
            instrument(original_actor),
            instrument(original_auth),
        )
        fixture.store.metrics.clear()
    pg_root = None
    if backend == "postgres":
        pg_root = int((fixture.cluster.data / "postmaster.pid").read_text().splitlines()[0])
    errors, operator = [], []
    stop = threading.Event()
    samples = threading.Thread(target=sample_processes, args=(ledger, stop, pg_root))
    samples.start()
    try:
        with fixture.listener(tls=True):
            for i in range(1, 5):
                fixture.bind(f"phone{i}", str(12345 + i))
                fixture.policy(
                    "grant",
                    {
                        "principal": f"phone{i}",
                        "project": "p",
                        "expected_version": 1,
                        "starts": None,
                        "expires": None,
                    },
                )
            clients = [Client(fixture, ledger, i) for i in range(5)]
            for client in clients:
                client.login("initial_login")
            epoch = time.monotonic()
            paths = [
                "overview",
                "tasks?limit=50",
                "tasks/task-000",
                "memories?limit=50",
                "memories?limit=50&q=retry",
                "graph",
            ]

            def viewer(client):
                cursor = None
                iteration = 0
                next_allowed = epoch + client.number / 5
                while not stop.is_set():
                    scheduled = max(epoch + client.number / 5 + iteration, next_allowed)
                    stop.wait(max(0, scheduled - time.monotonic()))
                    if time.monotonic() >= epoch + args.duration or stop.is_set():
                        return
                    try:
                        next_allowed = time.monotonic() + 1
                        path = paths[iteration % len(paths)]
                        # Exercise opaque pagination, resetting on epoch/lease change.
                        if iteration % 12 == 9 and cursor:
                            path = "memories?limit=50&cursor=" + cursor
                        ledger.write(
                            "schedule",
                            viewer=client.number,
                            lag_ms=max(0, time.monotonic() - scheduled) * 1000,
                        )
                        status, response = client.request(
                            "/viewer/api/v1/projects/p/" + path, phase="measured"
                        )
                        if status == 200:
                            if response["data"]["kind"] == "memories" and "q=" not in path:
                                cursor = response["next_cursor"]
                        elif status in (401, 409):
                            cursor = None
                            if status == 401 or response.get("error") != "invalid_cursor":
                                client.login("session_recovery")
                        elif status != 503:
                            raise AssertionError(f"unexpected status {status}")
                    except Exception as exc:
                        errors.append({"viewer": client.number, "error": type(exc).__name__})
                        ledger.write(
                            "client_failure", viewer=client.number, error=type(exc).__name__
                        )
                        stop.set()
                    iteration += 1

            def policy_change():
                if stop.wait(args.duration / 2):
                    return
                try:
                    before = time.monotonic()
                    with fixture.login_lock:
                        fixture.policy(
                            "revoke", {"principal": "phone4", "project": "p", "expected_version": 2}
                        )
                        # Use a copied old session. The affected client may concurrently
                        # recover; this independent probe must never regain permission.
                        probe = Client(fixture, ledger, 4)
                        probe.cookies = dict(clients[4].cookies)
                        status, _ = probe.request(
                            "/viewer/api/v1/projects/p/overview", phase="revoked_probe"
                        )
                        assert status in (401, 403, 404, 409)
                        fixture.policy(
                            "grant",
                            {
                                "principal": "phone4",
                                "project": "p",
                                "expected_version": 3,
                                "starts": None,
                                "expires": None,
                            },
                        )
                    operator.append(
                        {
                            "revoked_read_status": status,
                            "milliseconds": (time.monotonic() - before) * 1000,
                        }
                    )
                    ledger.write("policy_change", **operator[-1])
                except Exception as exc:
                    errors.append({"operator": type(exc).__name__})
                    stop.set()

            workers = [threading.Thread(target=viewer, args=(client,)) for client in clients]
            changer = threading.Thread(target=policy_change)
            changer.start()
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=args.duration + 15)
            changer.join(timeout=15)
            assert not any(worker.is_alive() for worker in workers) and not changer.is_alive()
            elapsed = time.monotonic() - epoch
            for client in clients:
                assert SESSION_COOKIE in client.cookies
            ledger.write(
                "interval_end",
                measured_seconds=elapsed,
                sessions=len(fixture.auth.sessions),
                pending_logins=len(fixture.auth.pending),
                contexts=len(fixture.auth.contexts),
            )
            if backend == "postgres":
                ledger.write("postgres_pool", **fixture.pool.get_stats())
    finally:
        stop.set()
        samples.join(timeout=10)
        ledger.close()
    rows = [json.loads(line) for line in (output / "measurements.jsonl").read_text().splitlines()]
    reads = [r for r in rows if r["kind"] == "request" and r["phase"] == "measured"]
    success = [r for r in reads if r["status"] == 200 and not r["error"]]
    resources = [r for r in rows if r["kind"] == "resources"]
    by_route = defaultdict(list)
    for r in success:
        by_route[r["route"]].append(r["ms"])
    latency = distribution([r["ms"] for r in success])
    summary = {
        "backend": backend,
        "corpus": corpus,
        "viewers": 5,
        "requested_seconds": args.duration,
        "measured_seconds": elapsed,
        "requested_rate_per_second": 5,
        "pacing": "five staggered closed-loop viewers, at most one read/viewer/second; no catch-up claim",
        "read_latency_ms": latency,
        "all_read_latency_ms": distribution([r["ms"] for r in reads]),
        "per_route_ms": {k: distribution(v) for k, v in by_route.items()},
        "status_counts": dict(Counter(str(r["status"]) for r in reads)),
        "body_bytes": sum(r["body_bytes"] for r in reads),
        "max_body_bytes": max(r["body_bytes"] for r in reads),
        "outbound_definition": "HTTP response body bytes only; excludes headers/TLS framing",
        "resource_definition": "harness+HTTP server process and its children plus disposable PostgreSQL tree; ps RSS sum/%cpu estimate",
        "rss_bytes": distribution([r["rss_bytes"] for r in resources]),
        "rss_first_bytes": resources[0]["rss_bytes"],
        "rss_last_bytes": resources[-1]["rss_bytes"],
        "cpu_percent_ps": distribution([r["cpu_percent_ps"] for r in resources]),
        "sqlite_lock_wait_ms": distribution(
            [r["wait_ms"] for r in rows if r["kind"] == "sqlite_lock"]
        ),
        "postgres_gate_wait_ms": distribution(
            [
                r["gate_wait_ms"]
                for r in rows
                if r["kind"] == "postgres_transaction" and "gate_wait_ms" in r
            ]
        ),
        "postgres_clock_wait_ms": distribution(
            [
                r["clock_wait_ms"]
                for r in rows
                if r["kind"] == "postgres_transaction" and "clock_wait_ms" in r
            ]
        ),
        "database_wait_definition": "SQLite delegated canonical lock admission; PostgreSQL native gate/clock timings include statement overhead, not pure lock wait",
        "schedule_lag_ms": distribution([r["lag_ms"] for r in rows if r["kind"] == "schedule"]),
        "policy_change": operator,
        "errors": errors,
        "session_recovery_requests": sum(
            r["kind"] == "request" and r["phase"] == "session_recovery" for r in rows
        ),
        "phone_refresh_under_three_seconds": "not_evaluated_no_physical_phone_or_tailnet",
        "finite_run_boundedness": "Per-response limits asserted; finite RSS samples do not prove unlimited-duration stability",
        "read_p95_under_one_second": latency["p95"] is not None and latency["p95"] < 1000,
        "functional_passed": not errors
        and bool(operator)
        and bool(success)
        and all(r["status"] in (200, 401, 409) and not r["error"] for r in reads),
        "full_envelope": not args.smoke and args.duration >= 1800 and elapsed >= 1800,
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--postgres-bin", type=Path, required=True)
    parser.add_argument("--duration", type=int, default=1800)
    parser.add_argument("--backend", choices=("both", "sqlite", "postgres"), default="both")
    parser.add_argument(
        "--smoke", action="store_true", help="Small corpus; never qualifies the envelope"
    )
    args = parser.parse_args()
    if not 10 <= args.duration <= 3600 or (not args.smoke and args.duration < 1800):
        parser.error("Use 1800-3600 seconds for qualification, or explicit --smoke for 10+ seconds")
    if not args.postgres_bin.is_absolute() or not (args.postgres_bin / "pg_ctl").is_file():
        parser.error("An actual absolute native PostgreSQL runtime is mandatory")
    args.output.mkdir(parents=True, exist_ok=False)
    os.environ["MEMORYKING_PG_BIN"] = str(args.postgres_bin)
    import test_viewer_auth_http as fixtures

    result = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "working_tree_changes": bool(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()
        ),
        "scope": "Synthetic native loopback direct TLS, controlled provider; sequential backends; no Docker/phone/tailnet qualification",
        "results": [],
        "passed": False,
    }
    try:
        for name, cls in (
            ("sqlite", fixtures.LocalViewerAuthHTTP),
            ("postgres", fixtures.PostgresViewerAuthHTTP),
        ):
            if args.backend not in ("both", name):
                continue
            cls.setUpClass()
            fixture = cls()
            try:
                fixture.setUp()
                try:
                    summary = run_backend(fixture, name, args, args.output / name)
                    result["results"].append(summary)
                    print(
                        json.dumps(
                            {
                                "backend": name,
                                "functional_passed": summary["functional_passed"],
                                "p95_ms": summary["read_latency_ms"]["p95"],
                            }
                        ),
                        flush=True,
                    )
                finally:
                    fixture.tearDown()
            finally:
                cls.tearDownClass()
        result["passed"] = all(
            r["functional_passed"] and r["read_p95_under_one_second"] for r in result["results"]
        )
    finally:
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
