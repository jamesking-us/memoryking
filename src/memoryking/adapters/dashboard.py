"""Opt-in owner dashboard; inspection and explicit domain review, never a scheduler."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from memoryking.adapters.local_store import LocalStore, uid
from memoryking.contracts.canonical import canonical_bytes, canonical_hash, load_strict
from memoryking.domain.local import LocalError, Released

STATIC = Path(__file__).with_name("web")
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css"),
    "/elephant.png": ("elephant.png", "image/png"),
    **{
        f"/{name}.js": (f"{name}.js", "text/javascript")
        for name in ("app", "observatory", "graph", "graph-layout", "graph-worker", "brand")
    },
    **{
        f"/{name}.svg": (f"{name}.svg", "image/svg+xml")
        for name in ("crown", "favicon", "wordmark", "crown-mono")
    },
}
SAFE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; worker-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class PlanFiles:
    """Explicit file adapter: bounded, allowlisted, hash-observed and read-only."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def read(self, relative: str, limit: int = 1048576) -> bytes:
        path = self.root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise LocalError("plan_source_invalid")
        if not path.is_file() or path.stat().st_size > limit:
            raise LocalError("plan_source_unavailable")
        raw = path.read_bytes()
        if len(raw) > limit:
            raise LocalError("plan_source_invalid")
        return raw

    def json(self, relative: str) -> dict[str, Any]:
        value = load_strict(self.read(relative))
        if not isinstance(value, dict):
            raise LocalError("plan_source_invalid")
        return value

    def artifacts(self) -> dict[str, tuple[str, str]]:
        result = {"roadmap": ("docs/IMPLEMENTATION-ROADMAP.md", "text/plain; charset=utf-8")}
        for n in range(1, 13):
            for extension, content_type in (
                ("pdf", "application/pdf"),
                ("epub", "application/epub+zip"),
            ):
                result[f"P{n:02d}-{extension}"] = (
                    f"docs/reports/phase-{n:02d}-report.{extension}",
                    content_type,
                )
            for label, file in {
                "acceptance": "acceptance.json",
                "verification": "local-job/job.json",
                "manifest": "manifest.json",
                "report-qa": "report-qa.json",
                "submission": "kindle-submission.json",
            }.items():
                result[f"P{n:02d}-{label}"] = (
                    f"docs/reports/evidence/phase-{n:02d}/{file}",
                    "text/plain; charset=utf-8",
                )
        for path in sorted((self.root / "docs/adr").glob("[0-9][0-9][0-9]-*.md"))[:40]:
            result[path.stem] = (str(path.relative_to(self.root)), "text/plain; charset=utf-8")
        return result

    def snapshot(self, at: int) -> dict[str, Any]:
        # The product dashboard operates without a development-report checkout.
        # A partially configured or corrupt legacy checkout must still fail closed.
        if not any(
            (self.root / path).exists()
            for path in ("docs/dashboard-plan.json", "docs/reports/phases.json")
        ):
            return {
                "schema_version": 1,
                "plan_id": "workspace",
                "plan_revision": "none",
                "source_hash": canonical_hash({}),
                "register_hash": canonical_hash({}),
                "observed_ms": at,
                "fresh_until_ms": at + 15000,
                "adapter": "workspace / current authorized inspection",
                "phases": [],
                "adrs": [],
                "counts": {
                    "closed": 0,
                    "report_closeouts": 0,
                    "total": 0,
                    "historical_jobs_passed": 0,
                },
                "activity": {
                    "status": "unknown",
                    "reason": "Connect a project to inspect its tasks",
                },
            }
        plan = self.json("docs/dashboard-plan.json")
        register = self.json("docs/reports/phases.json")
        if (
            plan.get("version") != 1
            or len(plan.get("phases", [])) != 12
            or register.get("schema_version") != 1
        ):
            raise LocalError("plan_source_invalid")
        if [p.get("id") for p in plan["phases"]] != [f"P{i:02d}" for i in range(1, 13)]:
            raise LocalError("plan_source_invalid")
        phases = []
        for index, phase in enumerate(plan["phases"], 1):
            state = register["phases"].get(str(index), {"status": "unknown"})
            status = state.get("status", "unknown")
            if status not in {
                "not_started",
                "in_progress",
                "closeout_pending",
                "complete",
                "partial",
                "blocked",
                "unknown",
            }:
                raise LocalError("plan_source_invalid")
            evidence: dict[str, Any] = {
                "report": "not_generated",
                "verification": "not_evaluated",
                "smtp": "not_submitted",
                "device": "unverified",
                "links": [],
            }
            if status in {"complete", "partial", "blocked"}:
                try:
                    prefix = f"docs/reports/evidence/phase-{index:02d}/"
                    qa, receipt = (
                        self.json(prefix + "report-qa.json"),
                        self.json(prefix + "kindle-submission.json"),
                    )
                    files = {
                        "source": f"docs/reports/phase-{index:02d}-report.md",
                        "pdf": f"docs/reports/phase-{index:02d}-report.pdf",
                        "epub": f"docs/reports/phase-{index:02d}-report.epub",
                        "readme": prefix + "README.snapshot.md",
                        "evidence": prefix + "manifest.json",
                    }
                    hashes = {
                        key: hashlib.sha256(self.read(path, 4194304)).hexdigest()
                        for key, path in files.items()
                    }
                    if (
                        hashes != qa["hashes"]
                        or hashes != receipt["hashes"]
                        or not all(
                            qa.get(k) is True
                            for k in (
                                "pdf_reviewed",
                                "epub_reviewed",
                                "evidence_reviewed",
                                "readme_reviewed",
                            )
                        )
                        or receipt["status"] != "smtp_accepted"
                    ):
                        raise LocalError("report_evidence_invalid")
                    manifest = self.json(prefix + "manifest.json")
                    job_path = prefix + "local-job/job.json"
                    expected = next(
                        (f["sha256"] for f in manifest["files"] if f["path"] == job_path), None
                    )
                    raw_job = self.read(job_path)
                    if hashlib.sha256(raw_job).hexdigest() != expected:
                        raise LocalError("report_evidence_invalid")
                    job = json.loads(raw_job)
                    if not isinstance(job, dict):
                        raise LocalError("report_evidence_invalid")
                    evidence = {
                        "report": "qa_passed",
                        "verification": "passed_at_revision"
                        if job.get("passed") is True
                        else "failed",
                        "code_revision": job["revision"],
                        "platform": job["platform"],
                        "smtp": receipt["status"],
                        "device": receipt.get("device_delivery", "unverified"),
                        "links": [
                            f"/api/artifact/P{index:02d}-pdf",
                            f"/api/artifact/P{index:02d}-epub",
                        ],
                        "source_hash": hashes["source"],
                        "checks": [
                            {
                                "name": step["name"],
                                "exit_code": step["exit_code"],
                                "duration_ms": round(step["seconds"] * 1000),
                            }
                            for step in job.get("steps", [])[:32]
                        ],
                    }
                    for label, file in {
                        "acceptance": "acceptance.json",
                        "verification": "local-job/job.json",
                        "manifest": "manifest.json",
                        "report-qa": "report-qa.json",
                        "submission": "kindle-submission.json",
                    }.items():
                        if (self.root / prefix / file).is_file():
                            evidence["links"].append(f"/api/artifact/P{index:02d}-{label}")
                except (LocalError, KeyError, TypeError, ValueError, OSError):
                    evidence = {
                        "report": "invalid_or_missing",
                        "verification": "unknown",
                        "smtp": "unknown",
                        "device": "unverified",
                        "links": [],
                    }
            phases.append(
                {
                    **phase,
                    "execution": status,
                    "started_at": state.get("started_at"),
                    "closed_at": state.get("closed_at"),
                    "evidence": evidence,
                }
            )
        return {
            "schema_version": 1,
            "plan_id": plan["id"],
            "plan_revision": plan["revision"],
            "source_hash": canonical_hash(plan),
            "register_hash": canonical_hash(register),
            "observed_ms": at,
            "fresh_until_ms": at + 15000,
            "adapter": "local-plan-files / read only",
            "phases": phases,
            "counts": {
                "closed": sum(
                    p["execution"] == "complete" and p["evidence"]["report"] == "qa_passed"
                    for p in phases
                ),
                "report_closeouts": sum(
                    p["execution"] in {"complete", "partial", "blocked"}
                    and p["evidence"]["report"] == "qa_passed"
                    and p["evidence"]["smtp"] == "smtp_accepted"
                    for p in phases
                ),
                "total": 12,
                "historical_jobs_passed": sum(
                    p["evidence"]["verification"] == "passed_at_revision" for p in phases
                ),
            },
            "activity": {
                "status": "unknown",
                "reason": "Choose a project in Task activity for reference-harness observations",
            },
            "adrs": [
                {"id": k, "url": "/api/artifact/" + k}
                for k in self.artifacts()
                if re.match(r"\d{3}-", k)
            ],
        }


class Dashboard(HTTPServer):
    request_queue_size = 8

    def __init__(self, store: LocalStore, root: Path, port: int = 0):
        self.store, self.files = store, PlanFiles(root)
        self.codes: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        super().__init__(("127.0.0.1", port), DashboardHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.host = f"127.0.0.1:{self.server_port}"
        self.thread: threading.Thread | None = None

    def issue(self, credential: str) -> dict[str, Any]:
        with self.store.transaction() as decision:
            who = self.store._auth(credential, "admin", "*", decision.evaluated_ms)
            self.codes = {
                k: v for k, v in self.codes.items() if v["expires"] > decision.evaluated_ms
            }
            if len(self.codes) >= 32:
                raise LocalError("bootstrap_limit")
            code = secrets.token_urlsafe(32)
            self.codes[hashlib.sha256(code.encode()).hexdigest()] = {
                "credential": credential,
                "tenant": who["tenant"],
                "expires": decision.evaluated_ms + 60000,
                "epoch": self.epoch(who["tenant"]),
            }
        self.store._expose()
        return {"url": self.origin, "code": code, "expires_ms": decision.evaluated_ms + 60000}

    def epoch(self, tenant: str) -> int:
        return int(
            self.store.db.execute(
                "SELECT policy_epoch FROM tenants WHERE tenant=?", (tenant,)
            ).fetchone()[0]
        )

    def current(self, session: str, at: int) -> dict[str, Any]:
        value = self.sessions.get(session)
        if value is None or value["expires"] <= at:
            raise LocalError("session_expired")
        who = self.store._auth(value["credential"], "admin", "*", at)
        if (
            value["epoch"] != self.epoch(who["tenant"])
            or value["incarnation"] != self.store.incarnation
        ):
            self.sessions.pop(session, None)
            raise LocalError("session_changed")
        return value

    def start(self) -> None:
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.thread:
            self.shutdown()
            self.thread.join(timeout=5)
        self.server_close()


class DashboardHandler(BaseHTTPRequestHandler):
    server: Dashboard
    server_version = "MemoryKing"

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Never log bootstrap codes, cookies, paths or source content.

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(5)

    def send(
        self,
        status: int,
        raw: bytes,
        content_type: str = "application/json",
        cookie: str | None = None,
        access_receipt: str | None = None,
    ) -> None:
        self.send_response(status)
        for name, value in SAFE_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        if access_receipt:
            self.send_header("X-MemoryKing-Access-Receipt", access_receipt)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def guard(self) -> None:
        if self.headers.get_all("Host", []) != [self.server.host]:
            raise LocalError("host_rejected")
        origins = self.headers.get_all("Origin", [])
        if origins and origins != [self.server.origin]:
            raise LocalError("origin_rejected")
        public_asset = self.command in {"GET", "HEAD"} and self.path in ASSETS
        if not public_asset and self.headers.get("Sec-Fetch-Site") not in {
            None,
            "same-origin",
            "none",
        }:
            raise LocalError("origin_rejected")
        agent_endpoint = self.path == "/api/v1/retrieval" and self.command == "POST"
        if (
            self.command not in {"GET", "HEAD"}
            and not agent_endpoint
            and origins != [self.server.origin]
        ):
            raise LocalError("origin_required")

    def session(self) -> str:
        if len(self.headers.get_all("Cookie", [])) != 1:
            raise LocalError("session_required")
        cookie = SimpleCookie()
        cookie.load(self.headers["Cookie"])
        if "mk_session" not in cookie:
            raise LocalError("session_required")
        return cookie["mk_session"].value

    def body(self, limit: int = 4096) -> dict[str, Any]:
        if self.headers.get_all("Content-Type", []) != ["application/json"] or self.headers.get(
            "Transfer-Encoding"
        ):
            raise LocalError("invalid_request")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isdigit() or not 1 <= int(lengths[0]) <= limit:
            raise LocalError("invalid_request")
        value = load_strict(self.rfile.read(int(lengths[0])))
        if not isinstance(value, dict):
            raise LocalError("invalid_request")
        return value

    def route(self) -> None:
        self.guard()
        dashboard, store = self.server, self.server.store
        if self.path == "/api/v1/retrieval" and self.command == "POST":
            from memoryking.adapters.retrieval import Retrieval

            authorization = self.headers.get_all("Authorization", [])
            if len(authorization) != 1 or not authorization[0].startswith("Bearer "):
                raise LocalError("bearer_required")
            # Cookies never authorize agent endpoints; reject mixed contexts.
            if self.headers.get_all("Cookie", []):
                raise LocalError("mixed_authentication")
            response = Retrieval(store).execute(authorization[0][7:], self.body(131072))
            response.emit(lambda b: self.send(200, b, access_receipt=response.access_receipt))
            return
        if self.command in {"GET", "HEAD"} and self.path in ASSETS:
            name, kind = ASSETS[self.path]
            self.send(200, (STATIC / name).read_bytes(), kind)
            return
        if self.command == "OPTIONS":
            if self.headers.get("Access-Control-Request-Method") not in {
                "GET",
                "HEAD",
                "POST",
            } or self.headers.get("Access-Control-Request-Headers", "").lower() not in {
                "",
                "content-type",
            }:
                raise LocalError("preflight_rejected")
            self.send(204, b"")
            return
        if self.command == "POST" and self.path == "/api/bootstrap":
            data = self.body()
            if set(data) != {"code"} or not isinstance(data["code"], str):
                raise LocalError("invalid_request")
            with store.transaction() as decision:
                code = dashboard.codes.pop(hashlib.sha256(data["code"].encode()).hexdigest(), None)
                if code is None or code["expires"] <= decision.evaluated_ms:
                    raise LocalError("bootstrap_invalid")
                who = store._auth(code["credential"], "admin", "*", decision.evaluated_ms)
                if code["epoch"] != dashboard.epoch(who["tenant"]):
                    raise LocalError("bootstrap_invalid")
                dashboard.sessions = {
                    k: v
                    for k, v in dashboard.sessions.items()
                    if v["expires"] > decision.evaluated_ms
                }
                if len(dashboard.sessions) >= 16:
                    raise LocalError("session_limit")
                session = secrets.token_urlsafe(32)
                dashboard.sessions[session] = {
                    **code,
                    "expires": decision.evaluated_ms + 900000,
                    "csrf": secrets.token_urlsafe(32),
                    "incarnation": store.incarnation,
                }
                raw = canonical_bytes({"ok": True})
            store._expose()
            Released(raw, decision, store.clock).emit(
                lambda b: self.send(
                    200,
                    b,
                    cookie=f"mk_session={session}; HttpOnly; SameSite=Strict; Path=/; Max-Age=900",
                )
            )
            return
        session = self.session()
        if self.command == "POST" and self.path == "/api/command":
            from memoryking.adapters.browser_commands import execute

            if self.headers.get_all("Authorization", []):
                raise LocalError("mixed_authentication")

            def current_session() -> dict[str, Any]:
                value = dashboard.sessions.get(session)
                if value is None:
                    raise LocalError("session_expired")
                return value

            response = execute(store, current_session(), self.body(131072), current_session)
            response.emit(lambda b: self.send(200, b, access_receipt=response.access_receipt))
            return
        with store.transaction() as decision:
            value = dashboard.current(session, decision.evaluated_ms)
            cookie = None
            content_type = "application/json"
            if self.command == "POST" and self.path == "/api/logout":
                data = self.body()
                if (
                    set(data) != {"csrf"}
                    or not isinstance(data["csrf"], str)
                    or not secrets.compare_digest(data["csrf"], value["csrf"])
                ):
                    raise LocalError("csrf_rejected")
                dashboard.sessions.pop(session, None)
                raw = b'{"ok":true}'
                cookie = "mk_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
            elif self.command in {"GET", "HEAD"} and self.path == "/api/snapshot":
                result = dashboard.files.snapshot(decision.evaluated_ms)
                result.update(
                    csrf=value["csrf"],
                    incarnation=store.incarnation,
                    principal=store._decode(value["credential"], "credential")["principal"],
                    tenant=value["tenant"],
                    authorization_epoch=dashboard.epoch(value["tenant"]),
                )
                raw = canonical_bytes(result)
            elif self.command in {"GET", "HEAD"} and self.path.startswith("/api/execution/"):
                from memoryking.adapters.execution import Execution
                from memoryking.adapters.local_store import identifier

                project = self.path.removeprefix("/api/execution/")
                identifier(project)
                who = store._auth(value["credential"], "read", project, decision.evaluated_ms)
                raw = canonical_bytes(
                    Execution(store).snapshot(who, project, decision.evaluated_ms)
                )
            elif self.command in {"GET", "HEAD"} and self.path.startswith(
                ("/api/receipts/", "/api/receipt/")
            ):
                from memoryking.adapters.local_store import identifier
                from memoryking.adapters.retrieval import Retrieval

                parts = self.path.split("/")
                if len(parts) not in {4, 5}:
                    raise LocalError("invalid_request")
                project = parts[3]
                identifier(project)
                who = store._auth(value["credential"], "read", project, decision.evaluated_ms)
                retrieval = Retrieval(store)
                if parts[2] == "receipts" and len(parts) == 4:
                    raw = canonical_bytes(
                        {"items": retrieval.list_receipts(who, project, decision.evaluated_ms)}
                    )
                elif parts[2] == "receipt" and len(parts) == 5:
                    identifier(parts[4])
                    row = retrieval._artifact(who, project, parts[4], decision.evaluated_ms)
                    raw = canonical_bytes(
                        {
                            "receipt": row["id"],
                            "rendered_hash": row["body_hash"],
                            "expires_ms": row["expires"],
                            "diagnostics": json.loads(row["diagnostics"]),
                        }
                    )
                else:
                    raise LocalError("route_unavailable")
            elif self.command in {"GET", "HEAD"} and self.path.startswith("/api/artifact/"):
                name = self.path.removeprefix("/api/artifact/")
                entry = dashboard.files.artifacts().get(name)
                if entry is None:
                    raise LocalError("unavailable")
                path, content_type = entry
                raw = dashboard.files.read(path, 4194304)
            else:
                raise LocalError("route_unavailable")
            who = store._decode(value["credential"], "credential")
            epochs = store.db.execute(
                "SELECT policy_epoch,purge_epoch FROM tenants WHERE tenant=?", (who["tenant"],)
            ).fetchone()
            store.db.execute(
                "INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    who["tenant"],
                    uid("rcpt"),
                    who["principal"],
                    store.incarnation,
                    decision.evaluated_ms,
                    decision.deadline_ms,
                    *epochs,
                    hashlib.sha256(raw).hexdigest(),
                    "[]",
                ),
            )
            store.fault("after_validation")
        store._expose()
        Released(raw, decision, store.clock).emit(lambda b: self.send(200, b, content_type, cookie))

    def do_GET(self) -> None:
        try:
            self.route()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except LocalError as exc:
            self.send(403, canonical_bytes({"error": exc.code}))
        except Exception:
            self.send(503, b'{"error":"dashboard_unavailable"}')

    do_HEAD = do_GET
    do_POST = do_GET
    do_OPTIONS = do_GET
    do_PUT = do_GET
    do_DELETE = do_GET
    do_PATCH = do_GET
