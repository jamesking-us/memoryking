"""Run the embedding worker inside the daemon for every configured partition.

With `serve --embedding-model DIR`, or an installed model the daemon finds on its own, it
loads the pinned local model once, embeds each query it briefs on, and completes the
indexing jobs of every experimental partition through the same authorized claim and
complete path an external worker uses. It also enables a partition for each project that
has memory but no partition yet, so semantic search follows the data; a project whose
partition an owner disabled is left alone. The loop talks to the daemon's own socket with
the owner credential it already holds, so nothing runs outside the ordinary authorization
checks.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from time import monotonic
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
        self.enabled_at = 0.0

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

    def enable_new_projects(self) -> None:
        """Create the missing partitions, at most once a minute and never above the limit."""
        if monotonic() - self.enabled_at < 60:
            return
        self.enabled_at = monotonic()
        with self.store.lock:
            missing = [
                row[0]
                for row in self.store.db.execute(
                    "SELECT DISTINCT o.project FROM observations o WHERE o.deleted=0 AND substr(o.project,1,6)!='_user_' AND NOT EXISTS(SELECT 1 FROM embedding_partitions p WHERE p.tenant=o.tenant AND p.project=o.project AND p.fingerprint=?) ORDER BY o.project LIMIT 16",
                    (self.provider.fingerprint,),
                ).fetchall()
            ]
        for project in missing:
            try:
                client = self.client(project)
                client.embedding(
                    {
                        "version": 1,
                        "incarnation": client.binding[-1],
                        "action": "configure",
                        "key": "auto-" + hashlib.sha256(project.encode()).hexdigest()[:16],
                        "args": {
                            "project": project,
                            "model": self.provider.name,
                            "state": "experimental",
                            "expected_generation": 0,
                        },
                    }
                )
                event("embeddings", "enabled", project=project)
            except LocalError as exc:
                event("embeddings", "enable_skipped", project=project, error=exc.code)
            except OSError as exc:
                failure("embeddings", exc, project=project)

    def client(self, project: str) -> Any:
        from memoryking.sdk import LocalClient

        if project not in self.clients:
            self.clients[project] = LocalClient(self.socket, self.credential_file, project)
        return self.clients[project]

    def tick(self) -> int:
        from memoryking.embedding_worker import run_once

        done = 0
        self.enable_new_projects()
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
