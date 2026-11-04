"""One fenced renderer per store root, outside canonical request transactions."""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from memoryking.adapters.local_files import Ownership, atomic, private_dir, private_file
from memoryking.adapters.report_store import Reports
from memoryking.application.store_core import StoreCore, uid
from memoryking.contracts.canonical import canonical_bytes, canonical_hash, load_strict
from memoryking.domain.local import LocalError
from memoryking.domain.reporting import ARTIFACT_LIMIT, BUILD_TIMEOUT, CHUNK_LIMIT
from memoryking.reporting.sandbox import render
from memoryking.reporting.template import reader_pages

OUTPUTS = {"report.pdf", "report.epub", "reader.json", "reader-pages.json"}


def build_one(
    store: StoreCore, credential: str, project: str, *, protected: Sequence[Path] = ()
) -> dict[str, Any] | None:
    """Trusted worker entry, using an explicitly granted report-author credential.

    Caller owns the store. A background service can invoke this without holding its
    request gate across rendering. A stale worker cannot publish a newer claim.
    """
    worker_root = store.root / "report-worker"
    private_dir(worker_root, create=True)
    owner = Ownership(worker_root)
    lease_file = worker_root / "lease.json"
    reports = Reports(store)
    key: tuple[Any, ...] | None = None
    token = uid("build")
    incarnation = store.incarnation
    try:
        if lease_file.exists():
            private_file(lease_file)
            lease = load_strict(lease_file.read_bytes())
            if not isinstance(lease, dict) or type(lease.get("until")) is not int:
                raise LocalError("report_worker_lease_invalid")
            if store.clock.utc_ms() < lease["until"]:
                raise LocalError("report_worker_busy")
        with tempfile.TemporaryDirectory(prefix="memoryking-render-") as folder:
            stage = Path(folder)
            stage.chmod(0o700)
            with store.transaction() as decision:
                who = store._auth(credential, "report_author", project, decision.evaluated_ms)
                # No renderer can still hold the process-level lease. Interrupted work
                # is failed explicitly; it is never silently rebuilt under a new token.
                store.db.execute(
                    "UPDATE report_revisions SET state='failed',token=NULL,lease_until=NULL WHERE tenant=? AND state='building'",
                    (who["tenant"],),
                )
                rows = store.db.execute(
                    "SELECT * FROM report_revisions WHERE tenant=? AND project=? AND state='draft' AND lease_until=0 ORDER BY created,report,revision LIMIT 10",
                    (who["tenant"], project),
                ).fetchall()
                row = None
                for candidate in rows:
                    if candidate["incarnation"] != store.incarnation or candidate["body"] is None:
                        continue
                    try:
                        reports._allowed(who, json.loads(candidate["body"]), decision.evaluated_ms)
                    except LocalError:
                        continue
                    row = candidate
                    break
                if row is None:
                    return None
                body = json.loads(row["body"])
                reports._allowed(who, body, decision.evaluated_ms)
                key = tuple(row[k] for k in ("tenant", "project", "report", "revision"))
                atomic(
                    lease_file,
                    canonical_bytes(
                        {
                            "token": token,
                            "until": decision.evaluated_ms + (BUILD_TIMEOUT + 10) * 1000,
                        }
                    ),
                )
                files = store.db.execute(
                    "SELECT name,blob FROM report_files WHERE tenant=? AND project=? AND report=? AND revision=? AND kind='input'",
                    key,
                ).fetchall()
                for file in files:
                    atomic(stage / file["name"], reports._bytes(who["tenant"], file["blob"]))
                atomic(stage / "request.json", canonical_bytes({"title": body["title"]}))
                store.db.execute(
                    "UPDATE report_revisions SET state='building',token=?,lease_until=? WHERE tenant=? AND project=? AND report=? AND revision=?",
                    (token, decision.evaluated_ms + (BUILD_TIMEOUT + 10) * 1000, *key),
                )
            store._expose()
            store.fault("report_after_claim")
            render(stage, protected=(store.root, store.control_path, *protected))
            store.fault("report_after_render")
            # Renderer output remains untrusted: parent independently checks exact
            # regular-file names, byte lengths, hashes and the approved reader model.
            result_path = stage / "result.json"
            private_file(result_path)
            if result_path.stat().st_size > CHUNK_LIMIT:
                raise LocalError("report_renderer_output_invalid")
            result = load_strict(result_path.read_bytes())
            if (
                not isinstance(result, dict)
                or set(result) != {"version", "artifacts", "reader_pages", "pdf_pages"}
                or result["version"] != 1
                or type(result["pdf_pages"]) is not int
                or not 1 <= result["pdf_pages"] <= 10000
            ):
                raise LocalError("report_renderer_output_invalid")
            items = result["artifacts"]
            if (
                not isinstance(items, list)
                or len(items) != len(OUTPUTS)
                or any(
                    not isinstance(item, dict) or set(item) != {"name", "sha256", "bytes"}
                    for item in items
                )
                or {item["name"] for item in items} != OUTPUTS
            ):
                raise LocalError("report_renderer_output_invalid")
            outputs: dict[str, bytes] = {}
            total = 0
            for item in items:
                path = stage / item["name"]
                private_file(path)
                size = path.stat().st_size
                total += size
                if size != item["bytes"] or total > ARTIFACT_LIMIT:
                    raise LocalError("report_artifact_limit")
                raw = path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != item["sha256"]:
                    raise LocalError("report_renderer_output_invalid")
                outputs[item["name"]] = raw
            document = load_strict(outputs["reader.json"])
            if not isinstance(document, dict) or canonical_hash(document) != body["model_sha256"]:
                raise LocalError("report_renderer_output_invalid")
            pages = reader_pages(document)
            if result["reader_pages"] != len(pages) or outputs[
                "reader-pages.json"
            ] != canonical_bytes([load_strict(page) for page in pages]):
                raise LocalError("report_renderer_output_invalid")
            if not outputs["report.pdf"].startswith(b"%PDF-") or not outputs[
                "report.epub"
            ].startswith(b"PK"):
                raise LocalError("report_renderer_output_invalid")
            with store.transaction() as decision:
                who = store._auth(credential, "report_author", project, decision.evaluated_ms)
                current = store.db.execute(
                    "SELECT * FROM report_revisions WHERE tenant=? AND project=? AND report=? AND revision=?",
                    key,
                ).fetchone()
                if (
                    store.incarnation != incarnation
                    or current is None
                    or current["state"] != "building"
                    or current["token"] != token
                    or current["lease_until"] <= decision.evaluated_ms
                ):
                    raise LocalError("report_worker_fenced")
                reports._allowed(who, body, decision.evaluated_ms)
                for name, raw in outputs.items():
                    digest = store._blob(who["tenant"], raw)
                    store.db.execute(
                        "INSERT INTO report_files VALUES (?,?,?,?,?,?,?,?)",
                        (*key, name, digest, len(raw), "artifact"),
                    )
                for index, raw in enumerate(pages):
                    digest = store._blob(who["tenant"], raw)
                    store.db.execute(
                        "INSERT INTO report_files VALUES (?,?,?,?,?,?,?,?)",
                        (*key, f"page-{index:06d}.json", digest, len(raw), "reader"),
                    )
                store.fault("report_before_manifest_commit")
                body.update(
                    pdf_pages=result["pdf_pages"],
                    reader_pages=len(pages),
                    renderer="memoryking-phase-v1",
                )
                store.db.execute(
                    "UPDATE report_revisions SET state='built',body=?,token=NULL,lease_until=NULL WHERE tenant=? AND project=? AND report=? AND revision=?",
                    (canonical_bytes(body).decode(), *key),
                )
                position = store._event(
                    who["tenant"],
                    "report_built",
                    str(key[2]),
                    decision.evaluated_ms,
                    who["principal"],
                )
                store._enqueue(who["tenant"], position, None, "system")
            store._expose()
            return {"report": key[2], "revision": key[3], "state": "built"}
    except BaseException:
        if key is not None and store.healthy:
            with store.transaction():
                store.db.execute(
                    "UPDATE report_revisions SET state='failed',token=NULL,lease_until=NULL WHERE tenant=? AND project=? AND report=? AND revision=? AND state='building' AND token=?",
                    (*key, token),
                )
            store._expose()
        raise
    finally:
        # render() waits for process termination, including its error paths. A
        # hard service crash leaves this lease to outlive the worker's own alarm.
        if key is not None and lease_file.exists():
            lease_file.unlink()
            from memoryking.adapters.local_files import sync_dir

            sync_dir(worker_root)
        owner.close()
