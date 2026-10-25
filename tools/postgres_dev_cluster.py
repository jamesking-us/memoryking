#!/usr/bin/env python3
"""Disposable private Unix-socket PostgreSQL fixture, never an existing database target."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


class DevCluster:
    def __init__(self, binary_directory: Path | None = None):
        self.bin = (
            binary_directory
            or Path(os.environ.get("MEMORYKING_PG_BIN", "tmp/postgres-dev/runtime/bin"))
        ).resolve()
        if not (self.bin / "pg_ctl").is_file():
            raise RuntimeError("Set MEMORYKING_PG_BIN to the isolated PostgreSQL bin directory")
        self.temporary = tempfile.TemporaryDirectory(prefix="mk-pg-", dir="/tmp")
        self.root = Path(self.temporary.name)
        self.data, self.socket = self.root / "data", self.root / "socket"
        self.socket.mkdir(mode=0o700)
        self.running = False
        try:
            self._run(
                "initdb",
                "-D",
                str(self.data),
                "-U",
                "mk_dev_admin",
                "--auth-local=trust",
                "--auth-host=reject",
                "--no-locale",
                "--encoding=UTF8",
            )
            self._run(
                "pg_ctl",
                "-D",
                str(self.data),
                "-l",
                str(self.root / "server.log"),
                "-o",
                f"-c listen_addresses='' -c unix_socket_directories={self.socket} -c unix_socket_permissions=0700 -c fsync=on -c synchronous_commit=on -c full_page_writes=on",
                "-w",
                "-t",
                "30",
                "start",
            )
            self.running = True
        except BaseException:
            self.close()
            raise

    def _run(self, program: str, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [str(self.bin / program), *args],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode:
            (self.root / f"{program}-failure.log").write_text(result.stdout + result.stderr)
            raise RuntimeError(f"Disposable PostgreSQL {program} failed; see {self.root}")
        return result

    def dsn(self, user: str = "mk_dev_admin", database: str = "postgres") -> str:
        # Fixed fixture identifiers, with private random socket path; no password/TCP.
        if not user.replace("_", "").isalnum() or not database.replace("_", "").isalnum():
            raise ValueError("Invalid fixture identifier")
        return f"host={self.socket} user={user} dbname={database}"

    def close(self) -> None:
        if self.running:
            self._run("pg_ctl", "-D", str(self.data), "-m", "immediate", "-w", "stop")
            self.running = False
        self.temporary.cleanup()

    def __enter__(self) -> DevCluster:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


if __name__ == "__main__":
    with DevCluster() as cluster:
        print(
            cluster._run(
                "psql",
                "-h",
                str(cluster.socket),
                "-U",
                "mk_dev_admin",
                "-d",
                "postgres",
                "-Atc",
                "SELECT version(); SHOW listen_addresses; SHOW fsync; SHOW synchronous_commit;",
            ).stdout
        )
