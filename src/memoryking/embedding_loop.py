"""Run the embedding worker inside the daemon for every configured partition.

With `serve --embedding-model DIR` the daemon loads the pinned local model once, embeds
each query it briefs on, and completes the indexing jobs of every experimental partition
through the same authorized claim and complete path an external worker uses. The loop
talks to the daemon's own socket with the owner credential it already holds, so nothing
runs outside the ordinary authorization checks.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from memoryking.domain.local import LocalError
from memoryking.observability import event, failure


class EmbeddingLoop:
    def __init__(
        self,
        store: Any,
        socket: Path,
        credential_file: Path,
        provider: Any,
        *,
        interval: float = 3.0,
        batch: int = 50,
    ):
        self.store, self.socket, self.credential_file = store, socket, credential_file
        self.provider, self.interval, self.batch = provider, interval, batch
        self.clients: dict[str, Any] = {}
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.run, name="memoryking-embeddings", daemon=True)
        self.completed = 0

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stopping.set()
        self.thread.join(timeout=10)

    def projects(self) -> list[str]:
        with self.store.lock:
            rows = self.store.db.execute(
                "SELECT DISTINCT project FROM embedding_partitions WHERE state='experimental' AND incarnation=? AND fingerprint=? ORDER BY project",
                (self.store.incarnation, self.provider.fingerprint),
            ).fetchall()
        return [row[0] for row in rows]

    def client(self, project: str) -> Any:
        from memoryking.sdk import LocalClient

        if project not in self.clients:
            self.clients[project] = LocalClient(self.socket, self.credential_file, project)
        return self.clients[project]

    def tick(self) -> int:
        from memoryking.embedding_worker import run_once

        done = 0
        for project in self.projects():
            try:
                client = self.client(project)
                while done < self.batch and not self.stopping.is_set():
                    result = run_once(client, self.provider)
                    if result.get("status") == "idle":
                        break
                    if (
                        result.get("status") not in {"complete", "indexed", "ok"}
                        and "error" in result
                    ):
                        event(
                            "embeddings", "attempt_failed", project=project, error=result["error"]
                        )
                        if result.get("status") == "reconciliation_required":
                            break
                    done += 1
            except LocalError as exc:
                if exc.code in {"history_changed", "context_changed"}:
                    self.clients.pop(project, None)
                event("embeddings", "paused", project=project, error=exc.code)
            except OSError as exc:
                failure("embeddings", exc, project=project)
        self.completed += done
        return done

    def run(self) -> None:
        event("embeddings", "ready", fingerprint=self.provider.fingerprint[:12])
        while not self.stopping.is_set():
            try:
                if self.tick() == 0:
                    self.stopping.wait(self.interval)
            except Exception as exc:  # the loop must outlive any single failure
                failure("embeddings", exc)
                self.stopping.wait(self.interval)
