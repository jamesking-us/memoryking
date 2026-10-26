"""Opt-in report scheduling with explicit scope and no implicit authority grants."""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from memoryking.adapters.local_files import private_file
from memoryking.adapters.local_store import LocalStore
from memoryking.adapters.report_build import build_one
from memoryking.adapters.report_delivery import Deliveries, MailConfiguration
from memoryking.contracts.canonical import load_strict
from memoryking.contracts.viewer import validate_viewer
from memoryking.domain.local import LocalError
from memoryking.reporting.sandbox import validate_protected_paths
from memoryking.reporting.smtp import GmailDestination


@dataclass(frozen=True)
class Configuration:
    enabled: bool
    tenant: str
    project: str
    credential: str = field(repr=False)
    protected: tuple[Path, ...] = field(repr=False)
    mail: MailConfiguration | None = field(default=None, repr=False)
    poll_seconds: int = 5

    @classmethod
    def from_file(cls, path: Path) -> Configuration:
        private_file(path)
        if path.stat().st_size > 8192:
            raise LocalError("invalid_report_worker_configuration")
        value = load_strict(path.read_bytes())
        validate_viewer("ReportWorkerConfiguration", value, reporting=True)
        paths = [path.resolve()]
        credential = ""
        if value["credential_file"] is not None:
            secret = Path(value["credential_file"])
            if not secret.is_absolute():
                raise LocalError("invalid_report_worker_configuration")
            private_file(secret)
            if secret.stat().st_size > 8192:
                raise LocalError("invalid_report_worker_configuration")
            credential = secret.read_text().strip()
            paths.append(secret.resolve())
        mail = None
        if value["mail_secret_file"] is not None:
            secret = Path(value["mail_secret_file"])
            if not secret.is_absolute():
                raise LocalError("invalid_report_worker_configuration")
            mail = MailConfiguration(
                GmailDestination.from_file(secret), value["sender_enabled"], value["project"]
            )
            paths.append(secret.resolve())
        if (value["enabled"] and not credential) or (
            value["sender_enabled"] and (not value["enabled"] or mail is None)
        ):
            raise LocalError("invalid_report_worker_configuration")
        validate_protected_paths(paths)
        return cls(
            value["enabled"],
            value["tenant"],
            value["project"],
            credential,
            tuple(paths),
            mail,
            value["poll_seconds"],
        )

    def attach(self, store: LocalStore) -> None:
        # Only the explicitly configured tenant receives this destination.
        if not self.enabled:
            return
        with store.transaction() as decision:
            who = store._auth(self.credential, "read", self.project, decision.evaluated_ms)
            if who["tenant"] != self.tenant:
                raise LocalError("invalid_report_worker_scope")
        store.report_mail = self.mail  # type: ignore[attr-defined]


class ReportWorker:
    def __init__(
        self,
        configuration: Configuration,
        open_store: Callable[[], AbstractContextManager[LocalStore]],
    ):
        self.configuration, self.open_store = configuration, open_store
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None
        self.mail_after = ""
        self.last_status: str | None = None

    def start(self) -> None:
        config = self.configuration
        if not config.enabled:
            return
        if self.thread is not None:
            raise LocalError("report_worker_already_started")
        with self.open_store() as store:
            validate_protected_paths((*config.protected, store.root, store.control_path))
            with store.transaction() as decision:
                who = store._auth(
                    config.credential, "report_author", config.project, decision.evaluated_ms
                )
                store._auth(config.credential, "read", config.project, decision.evaluated_ms)
                if who["tenant"] != config.tenant:
                    raise LocalError("invalid_report_worker_scope")
                if config.mail is not None and config.mail.enabled:
                    store._auth(
                        config.credential, "report_delivery", config.project, decision.evaluated_ms
                    )
        self.thread = threading.Thread(target=self._run, name="memoryking-reports", daemon=False)
        self.thread.start()

    def stop(self) -> None:
        self.stopping.set()
        if self.thread is not None:
            # Finish the already bounded render/SMTP operation before closing SQL.
            self.thread.join()

    def tick(self) -> dict[str, Any]:
        config = self.configuration
        if not config.enabled or self.stopping.is_set():
            return {"state": "disabled"}
        result: dict[str, Any] = {"state": "idle"}
        try:
            with self.open_store() as store:
                built = build_one(
                    store, config.credential, config.project, protected=config.protected
                )
                if built is not None:
                    result = {"state": "built"}
        except LocalError as error:
            result = {"state": "build_attention", "code": error.code}
        # A blocked build does not prevent an independent authorized delivery.
        if config.mail is not None and config.mail.enabled and not self.stopping.is_set():
            with self.open_store() as store:
                with store.transaction() as decision:
                    who = store._auth(
                        config.credential, "report_delivery", config.project, decision.evaluated_ms
                    )
                    rows = store.db.execute(
                        "SELECT id,body FROM report_deliveries WHERE tenant=? AND project=? AND state IN ('queued','submitting') AND id>? ORDER BY id LIMIT 32",
                        (who["tenant"], config.project, self.mail_after),
                    ).fetchall()
                    candidates = [dict(row) for row in rows]
                if not candidates:
                    self.mail_after = ""
                for row in candidates:
                    self.mail_after = row["id"]
                    body = json.loads(row["body"])
                    if body["state"] == "queued" and body["next_attempt_ms"] > store.clock.utc_ms():
                        continue
                    try:
                        delivered = Deliveries(store).attempt(
                            config.credential, config.project, row["id"]
                        )
                        result = {"state": delivered["state"]}
                        break  # At most one provider operation per tick.
                    except LocalError as error:
                        result = {"state": "delivery_attention", "code": error.code}
                        break  # Advance on the next tick, including uncertain completion errors.
        return result

    def _run(self) -> None:
        while not self.stopping.is_set():
            try:
                result = self.tick()
                # No document identifiers, paths, recipient details or provider text.
                state = str(result["state"])
            except Exception:
                state = "worker_attention"
            if state != self.last_status:
                print(json.dumps({"report_worker": state}), file=sys.stderr, flush=True)
                self.last_status = state
            self.stopping.wait(self.configuration.poll_seconds)
