"""Private home service: one storage owner, optional viewer/reports, no admin HTTP."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from contextlib import ExitStack, nullcontext
from pathlib import Path

from memoryking.adapters.local_files import private_file
from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
from memoryking.adapters.report_worker import Configuration, ReportWorker
from memoryking.adapters.viewer_config import load_config
from memoryking.adapters.viewer_http import ViewerListener
from memoryking.domain.local import LocalError


def socket_dsn(path: Path) -> str:
    """The optional home database has no TCP fallback, including multi-host DSNs."""
    from psycopg.conninfo import conninfo_to_dict

    private_file(path)
    with path.open("rb") as stream:
        raw = stream.read(8193)
    if not raw.strip() or len(raw) > 8192:
        raise LocalError("invalid_secret_file")
    value = raw.decode().strip()
    options = conninfo_to_dict(value)
    host = options.get("host", "")
    if (
        not isinstance(host, str)
        or not Path(host).is_absolute()
        or any(character.isspace() or character == "," for character in host)
        or "hostaddr" in options
        or "service" in options
        or any(os.environ.get(key) for key in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"))
    ):
        raise LocalError("home_database_socket_required")
    return value


def serve(args: argparse.Namespace, stopping: threading.Event) -> None:
    viewer_config = load_config(args.viewer_config) if args.viewer_config else None
    report_config = Configuration.from_file(args.report_config) if args.report_config else None
    if (args.backend == "shared") != bool(args.dsn_file) or not 2 <= args.pool_size <= 16:
        raise LocalError("invalid_home_configuration")
    if (
        viewer_config
        and report_config
        and report_config.enabled
        and viewer_config["tenant"] != report_config.tenant
    ):
        raise LocalError("invalid_home_configuration")
    with ExitStack() as cleanup:
        ipc_thread: threading.Thread | None = None
        socket_path: str | None = None
        if args.backend == "local":
            store = cleanup.enter_context(LocalStore(args.data, args.control))
            if report_config:
                report_config.attach(store)
            viewer = (
                ViewerListener(
                    viewer_config,
                    lambda _, deadline: nullcontext(store),
                    lambda deadline: nullcontext(store),
                )
                if viewer_config
                else None
            )
            worker = (
                ReportWorker(report_config, lambda: nullcontext(store)) if report_config else None
            )
            server = cleanup.enter_context(Server(store))
            socket_path = str(server.path)
            ipc_thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.1},
                name="memoryking-home-ipc",
                daemon=False,
            )
            ipc_thread.start()

            def stop_ipc() -> None:
                server.shutdown()
                assert ipc_thread is not None
                ipc_thread.join()

            cleanup.callback(stop_ipc)
        else:
            from memoryking.adapters.postgres_pool import create_pool
            from memoryking.adapters.shared_service import SharedService

            pool = create_pool(socket_dsn(args.dsn_file), size=args.pool_size)
            cleanup.callback(pool.close)
            service = SharedService(pool, args.data, args.control)
            service.report_configuration = report_config
            tenant = (
                viewer_config["tenant"]
                if viewer_config
                else report_config.tenant
                if report_config
                else "main"
            )
            # Even a disabled viewer must not report ready on missing, corrupt,
            # restoring or incompatible storage/control history.
            with service.viewer_auth_store(tenant, time.monotonic() + 5) as probe:
                with probe.transaction():
                    pass
            viewer = (
                ViewerListener(
                    viewer_config,
                    lambda session, deadline: service.store(
                        viewer_config["tenant"],
                        session.subject.credential,
                        request_deadline=deadline,
                    ),
                    lambda deadline: service.viewer_auth_store(viewer_config["tenant"], deadline),
                )
                if viewer_config
                else None
            )
            worker = (
                ReportWorker(
                    report_config,
                    lambda: service.store(report_config.tenant, report_config.credential),
                )
                if report_config
                else None
            )
        if viewer:
            cleanup.callback(viewer.stop)
            viewer.start()
        if worker:
            cleanup.callback(worker.stop)
            worker.start()
        print(
            json.dumps(
                {
                    "ready": True,
                    "backend": args.backend,
                    "viewer_enabled": bool(viewer_config and viewer_config["enabled"]),
                    "reports_enabled": bool(report_config and report_config.enabled),
                    "socket": socket_path,
                }
            ),
            flush=True,
        )
        while not stopping.wait(0.2):
            threads = [
                ipc_thread,
                viewer.thread if viewer else None,
                worker.thread if worker else None,
            ]
            if any(thread is not None and not thread.is_alive() for thread in threads):
                raise LocalError("home_service_thread_stopped")
        # ExitStack drains worker/listener/IPC before closing the owner or pool.
        # Operators must still wait for process exit before offline copying.


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--backend", choices=("local", "shared"), default="local")
    result.add_argument("--data", type=Path, required=True)
    result.add_argument("--control", type=Path, required=True)
    result.add_argument(
        "--dsn-file", type=Path, help="Shared profile only; private Unix-socket DSN"
    )
    result.add_argument("--pool-size", type=int, default=8, help="Shared pool capacity, 2..16")
    result.add_argument(
        "--viewer-config", type=Path, help="Optional private closed viewer configuration"
    )
    result.add_argument(
        "--report-config", type=Path, help="Optional private report worker configuration"
    )
    return result


def main() -> int:
    args = parser().parse_args()
    stopping = threading.Event()
    for event in (signal.SIGTERM, signal.SIGINT):
        signal.signal(event, lambda *_: stopping.set())
    try:
        serve(args, stopping)
    except LocalError as error:
        print(json.dumps({"error": error.code}), file=sys.stderr)
        return 1
    except Exception:
        # Driver/startup exceptions can embed connection strings. Never log them.
        print(json.dumps({"error": "home_startup_or_service_failed"}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
