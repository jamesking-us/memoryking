"""Canonical report records and current whole-document authorization.

Rendering and mail never run inside a content/policy transaction. Inputs are scoped,
chunked bytes, never paths. Independent policy records survive content rollback.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Any

from memoryking.adapters.knowledge_store import Knowledge
from memoryking.adapters.local_files import private_file
from memoryking.adapters.local_store import LocalStore, uid
from memoryking.contracts.canonical import canonical_bytes, canonical_hash, load_strict
from memoryking.contracts.viewer import validate_viewer
from memoryking.domain.local import LocalError, Released, temporal_valid
from memoryking.domain.reporting import BUILD_QUEUE_LIMIT, CHUNK_LIMIT, INPUT_LIMIT, SOURCE_LIMIT
from memoryking.reporting.template import model

CLASS = {"public": 0, "internal": 1, "restricted": 2}
CONTROL_ACTIONS = {"bind_source", "policy"}
READ_ACTIONS = {"status", "reader", "artifact", "delivery_status", "list", "image"}
CAPABILITIES = {
    "bind_source": "report_policy",
    "policy": "report_policy",
    "upload": "report_author",
    "submit": "report_author",
    "build": "report_author",
    "qa": "report_qa",
    "publish": "report_publish",
    "withdraw": "report_publish",
    "artifact": "export",
    "discard_upload": "report_author",
    "delivery_status": "report_delivery",
    "reconcile": "report_policy",
}


class Reports:
    def __init__(self, store: LocalStore):
        self.s, self.db = store, store.db

    def execute(self, credential: str, command: dict[str, Any]) -> Released:
        validate_viewer("ReportCommand", command, reporting=True)
        if command["incarnation"] != self.s.incarnation:
            raise LocalError("history_changed")
        action, args = command["action"], command["args"]
        with self.s.transaction() as decision:
            at = decision.evaluated_ms
            who = self.s._auth(credential, CAPABILITIES.get(action, "read"), args["project"], at)
            if action in CONTROL_ACTIONS:
                result = self._control(who, command, at)
            else:
                self.s._subject_auth(self.s._subject_token(who), "read", args["project"], at)
                target = None
                if "revision" in args:
                    target = self._target(who, args, at)
                prior = self.db.execute(
                    "SELECT request_hash,result FROM report_retries WHERE tenant=? AND actor=? AND incarnation=? AND key=?",
                    (who["tenant"], who["principal"], self.s.incarnation, command["key"]),
                ).fetchone()
                if prior is not None and action not in READ_ACTIONS:
                    if prior["request_hash"] != canonical_hash(command):
                        raise LocalError("idempotency_conflict")
                    result = json.loads(prior["result"])
                    if action == "submit":
                        self._target(who, {**args, "revision": result["revision"]}, at)
                else:
                    if action == "upload":
                        result = self._upload(who, args)
                    elif action == "discard_upload":
                        self.db.execute(
                            "DELETE FROM report_uploads WHERE tenant=? AND project=? AND actor=? AND incarnation=? AND upload=?",
                            (
                                who["tenant"],
                                args["project"],
                                who["principal"],
                                self.s.incarnation,
                                args["upload"],
                            ),
                        )
                        result = {"upload": args["upload"], "discarded": True}
                    elif action == "submit":
                        result = self._submit(who, args, at)
                    elif action == "status":
                        result = self._status(target)
                    elif action == "list":
                        result = self._list(who, args, at)
                    elif action == "image":
                        result = self._image(who, target, args)
                    elif action == "reader":
                        result = self._reader(who, target, args)
                    elif action == "artifact":
                        result = self._artifact(who, target, args)
                    elif action in {"delivery_status", "reconcile"}:
                        from memoryking.adapters.report_delivery import Deliveries

                        ledger = Deliveries(self.s)
                        if action == "reconcile":
                            result = ledger.reconcile(
                                who,
                                args,
                                at,
                                key=command["key"],
                                request_hash=canonical_hash(command),
                            )
                        else:
                            body = ledger._job(who["tenant"], args["delivery"])
                            if body["project"] != args["project"]:
                                raise LocalError("unavailable")
                            result = ledger._public(body)
                    else:
                        result = self._lifecycle(who, target, action, args, at)
                    if action not in READ_ACTIONS:
                        if action not in {"upload", "discard_upload", "reconcile"}:
                            position = self.s._event(
                                who["tenant"],
                                "report_" + action,
                                args["report"],
                                at,
                                who["principal"],
                            )
                            self.s._enqueue(who["tenant"], position, None, "system")
                        self.db.execute(
                            "INSERT INTO report_retries VALUES (?,?,?,?,?,?)",
                            (
                                who["tenant"],
                                who["principal"],
                                self.s.incarnation,
                                command["key"],
                                canonical_hash(command),
                                canonical_bytes(result).decode(),
                            ),
                        )
            result = {
                **result,
                "incarnation": self.s.incarnation,
                "evaluated_ms": at,
                "deadline_ms": decision.deadline_ms,
                "receipt": uid("rcpt"),
            }
            raw = canonical_bytes(result)
            if len(raw) > CHUNK_LIMIT:
                raise LocalError("report_response_limit")
            epochs = self.db.execute(
                "SELECT policy_epoch,purge_epoch FROM tenants WHERE tenant=?", (who["tenant"],)
            ).fetchone()
            self.db.execute(
                "INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    who["tenant"],
                    result["receipt"],
                    who["principal"],
                    self.s.incarnation,
                    at,
                    decision.deadline_ms,
                    *epochs,
                    canonical_hash(result),
                    "[]",
                ),
            )
            self.s.fault("after_validation")
        self.s._expose()
        return Released(raw, decision, self.s.clock)

    def _control(self, who: dict[str, Any], command: dict[str, Any], at: int) -> dict[str, Any]:
        tenant, actor = who["tenant"], who["principal"]
        prior = self.db.execute(
            "SELECT request_hash,result FROM report_control_retries WHERE tenant=? AND actor=? AND key=?",
            (tenant, actor, command["key"]),
        ).fetchone()
        if prior is not None:
            if prior["request_hash"] != canonical_hash(command):
                raise LocalError("idempotency_conflict")
            return dict(json.loads(prior["result"]))
        action, args = command["action"], command["args"]
        if action == "bind_source":
            body, table, column = args["binding"], "report_sources", "id"
            name = body["id"]
            if (
                body["starts"] is not None
                and body["expires"] is not None
                and body["starts"] >= body["expires"]
            ):
                raise LocalError("invalid_interval")
        else:
            body, table, column = args["policy"], "report_policies", "program"
            name = body["program"]
            if body["project"] != args["project"] or body["template"] != "phase-report":
                raise LocalError("invalid_report_policy")
            if body["channel"] == "app":
                if body["destination"] is not None or body["recipient_principal"] is not None:
                    raise LocalError("invalid_report_policy")
            else:
                from memoryking.adapters.report_delivery import configured

                config = configured(self.s, body["destination"], args["project"])
                if body["recipient_principal"] not in body["readers"]:
                    raise LocalError("invalid_report_policy")
                body = {**body, "recipient_fingerprint": config.destination.fingerprint}
        prior = self.db.execute(
            f"SELECT version FROM {table} WHERE tenant=? AND project=? AND {column}=?",
            (tenant, args["project"], name),
        ).fetchone()
        version = 0 if prior is None else prior["version"]
        if version != args["expected_version"]:
            raise LocalError("revision_conflict")
        result = {"id": name, "version": version + 1}
        record = self.s._journal(
            {
                "kind": "report_policy",
                "tenant": tenant,
                "actor": actor,
                "operation": action,
                "project": args["project"],
                "body": body,
                "key": command["key"],
                "request_hash": canonical_hash(command),
                "result": result,
            }
        )
        self.s._apply(record, at)
        return result

    def apply_control(self, action: dict[str, Any]) -> None:
        tenant, project, body, result = (
            action["tenant"],
            action["project"],
            action["body"],
            action["result"],
        )
        table, column = (
            ("report_sources", "id")
            if action["operation"] == "bind_source"
            else ("report_policies", "program")
        )
        self.db.execute(
            f"DELETE FROM {table} WHERE tenant=? AND project=? AND {column}=?",
            (tenant, project, result["id"]),
        )
        self.db.execute(
            f"INSERT INTO {table} VALUES (?,?,?,?,?)",
            (tenant, project, result["id"], result["version"], canonical_bytes(body).decode()),
        )
        self.db.execute(
            "INSERT OR IGNORE INTO report_control_retries VALUES (?,?,?,?,?)",
            (
                tenant,
                action["actor"],
                action["key"],
                action["request_hash"],
                canonical_bytes(result).decode(),
            ),
        )

    def _policy(self, tenant: str, project: str, program: str) -> tuple[int, dict[str, Any]]:
        row = self.db.execute(
            "SELECT version,body FROM report_policies WHERE tenant=? AND project=? AND program=?",
            (tenant, project, program),
        ).fetchone()
        if row is None:
            raise LocalError("report_policy_required")
        return row["version"], json.loads(row["body"])

    def _bytes(self, tenant: str, digest: str, limit: int = INPUT_LIMIT) -> bytes:
        path = self.s.root / "blobs" / tenant / digest
        private_file(path)
        if path.stat().st_size > limit:
            raise LocalError("report_input_limit")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise LocalError("blob_corrupt")
        return raw

    def _upload(self, who: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        try:
            raw = base64.b64decode(args["data_base64"], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise LocalError("invalid_report_input") from exc
        if (
            not raw
            or len(raw) > CHUNK_LIMIT
            or args["part"] >= args["parts"]
            or args["parts"] != (args["bytes"] + CHUNK_LIMIT - 1) // CHUNK_LIMIT
            or len(raw) != min(CHUNK_LIMIT, args["bytes"] - args["part"] * CHUNK_LIMIT)
        ):
            raise LocalError("invalid_report_input")
        if (
            args["name"] in {"source.md", "readme.md", "evidence.json"}
            and args["bytes"] > SOURCE_LIMIT
        ):
            raise LocalError("report_source_limit")
        key = (who["tenant"], args["project"], who["principal"], self.s.incarnation, args["upload"])
        rows = self.db.execute(
            "SELECT * FROM report_uploads WHERE tenant=? AND project=? AND actor=? AND incarnation=? AND upload=?",
            key,
        ).fetchall()
        if len({r["name"] for r in rows} | {args["name"]}) > 61:
            raise LocalError("report_input_limit")
        if sum(r["chunk_bytes"] for r in rows) + len(raw) > INPUT_LIMIT:
            raise LocalError("report_input_limit")
        for row in rows:
            if row["name"] == args["name"]:
                if (row["digest"], row["bytes"], row["parts"]) != (
                    args["sha256"],
                    args["bytes"],
                    args["parts"],
                ):
                    raise LocalError("report_input_conflict")
                if row["part"] == args["part"]:
                    if row["blob"] != hashlib.sha256(raw).hexdigest():
                        raise LocalError("report_input_conflict")
                    return {"upload": args["upload"], "name": args["name"], "part": args["part"]}
        # Bound unfinished input storage per author/project, including abandoned uploads.
        total = self.db.execute(
            "SELECT COALESCE(SUM(chunk_bytes),0) FROM report_uploads WHERE tenant=? AND project=? AND actor=?",
            key[:3],
        ).fetchone()[0]
        if total + len(raw) > INPUT_LIMIT:
            raise LocalError("report_input_limit")
        digest = self.s._blob(who["tenant"], raw)
        self.db.execute(
            "INSERT INTO report_uploads VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                *key,
                args["name"],
                args["part"],
                args["parts"],
                args["sha256"],
                args["bytes"],
                digest,
                len(raw),
            ),
        )
        return {"upload": args["upload"], "name": args["name"], "part": args["part"]}

    def _dependency(self, who: dict[str, Any], project: str, dep: dict[str, Any], at: int) -> str:
        if dep["kind"] == "external_source":
            row = self.db.execute(
                "SELECT body FROM report_sources WHERE tenant=? AND project=? AND id=?",
                (who["tenant"], project, dep["id"]),
            ).fetchone()
            if row is None:
                raise LocalError("report_dependency_unavailable")
            body = json.loads(row["body"])
            if (
                not body["enabled"]
                or not temporal_valid(at, body["starts"], body["expires"])
                or who["principal"] not in body["readers"]
                or dep["sha256"] != body["sha256"]
            ):
                raise LocalError("report_dependency_unavailable")
            return str(body["classification"])
        if dep["kind"] == "observation":
            row = self.db.execute(
                "SELECT * FROM observations WHERE tenant=? AND id=? AND project=?",
                (who["tenant"], dep["id"], project),
            ).fetchone()
            if row is None or not self.s._visible(who, row, at) or row["blob"] != dep["sha256"]:
                raise LocalError("report_dependency_unavailable")
            self._bytes(who["tenant"], row["blob"])
            return str(row["classification"])
        knowledge = Knowledge(self.s)
        row = knowledge._revision(who["tenant"], dep["id"], project)
        if (
            not knowledge._available(who, row, at)
            or hashlib.sha256(row["body"].encode()).hexdigest() != dep["sha256"]
        ):
            raise LocalError("report_dependency_unavailable")
        # Knowledge revisions are at least as restricted as every supporting observation.
        level = CLASS[json.loads(row["body"]).get("classification", "restricted")]
        for span in knowledge._evidence(who["tenant"], dep["id"]):
            source = self.db.execute(
                "SELECT classification FROM observations WHERE tenant=? AND id=?",
                (who["tenant"], span["observation"]),
            ).fetchone()
            level = max(level, CLASS[source["classification"]])
        return next(name for name, value in CLASS.items() if value == level)

    def _allowed(self, who: dict[str, Any], body: dict[str, Any], at: int) -> None:
        self.s._subject_auth(self.s._subject_token(who), "read", body["project"], at)
        _, policy = self._policy(who["tenant"], body["project"], body["program"])
        if (
            who["principal"] not in body["readers"]
            or who["principal"] not in policy["readers"]
            or CLASS[body["classification"]] > CLASS[policy["classification"]]
        ):
            raise LocalError("unavailable")
        for dep in body["dependencies"]:
            level = self._dependency(who, body["project"], dep, at)
            if CLASS[level] > CLASS[body["classification"]]:
                raise LocalError("report_classification_conflict")

    def _submit(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        tenant, project = who["tenant"], args["project"]
        version, policy = self._policy(tenant, project, args["program"])
        latest = (
            self.db.execute(
                "SELECT MAX(revision) FROM report_revisions WHERE tenant=? AND project=? AND report=?",
                (tenant, project, args["report"]),
            ).fetchone()[0]
            or 0
        )
        acknowledged = (
            self.db.execute(
                "SELECT MAX(revision) FROM report_deliveries WHERE tenant=? AND project=? AND report=?",
                (tenant, project, args["report"]),
            ).fetchone()[0]
            or 0
        )
        latest = max(latest, acknowledged)
        if latest != args["expected_revision"]:
            raise LocalError("revision_conflict")
        revision = latest + 1
        body = {
            k: args[k]
            for k in (
                "project",
                "report",
                "program",
                "phase",
                "task",
                "title",
                "technical_status",
                "classification",
                "dependencies",
            )
        }
        body.update(
            version=1,
            revision=revision,
            template="phase-report",
            template_version=1,
            review_status="pending",
            readers=policy["readers"],
            policy_version=version,
        )
        self._allowed(who, body, at)
        key = (tenant, project, who["principal"], self.s.incarnation, args["upload"])
        rows = self.db.execute(
            "SELECT * FROM report_uploads WHERE tenant=? AND project=? AND actor=? AND incarnation=? AND upload=? ORDER BY name,part",
            key,
        ).fetchall()
        groups: dict[str, list[Any]] = {}
        for row in rows:
            groups.setdefault(row["name"], []).append(row)
        if not {"source.md", "readme.md", "evidence.json"} <= groups.keys():
            raise LocalError("report_input_incomplete")
        files: dict[str, bytes] = {}
        for name, chunks in groups.items():
            if len(chunks) != chunks[0]["parts"] or [c["part"] for c in chunks] != list(
                range(len(chunks))
            ):
                raise LocalError("report_input_incomplete")
            raw = b"".join(self._bytes(tenant, chunk["blob"], CHUNK_LIMIT) for chunk in chunks)
            if (
                len(raw) != chunks[0]["bytes"]
                or hashlib.sha256(raw).hexdigest() != chunks[0]["digest"]
            ):
                raise LocalError("report_input_corrupt")
            files[name] = raw
        if sum(map(len, files.values())) > INPUT_LIMIT:
            raise LocalError("report_input_limit")
        try:
            document = model(files["source.md"].decode("utf-8"), args["title"])
            files["readme.md"].decode("utf-8")
            evidence = load_strict(files["evidence.json"])
        except (UnicodeError, ValueError) as exc:
            raise LocalError("invalid_report_input") from exc
        if not isinstance(evidence, dict) or not evidence:
            raise LocalError("report_evidence_required")
        figures = {block["reference"] for block in document["blocks"] if block["kind"] == "figure"}
        if set(files) != {"source.md", "readme.md", "evidence.json"} | figures:
            raise LocalError("report_figure_inputs_mismatch")
        hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
        covered = {dep["sha256"] for dep in body["dependencies"]}
        if any(digest not in covered for name, digest in hashes.items() if name != "source.md"):
            raise LocalError("report_source_binding_required")
        body.update(
            source_sha256=hashes["source.md"],
            model_sha256=canonical_hash(document),
            readme_sha256=hashes["readme.md"],
            evidence_sha256=hashes["evidence.json"],
            figures=sorted(figures),
        )
        self.db.execute(
            "INSERT INTO report_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                tenant,
                project,
                args["report"],
                revision,
                self.s.incarnation,
                "draft",
                who["principal"],
                at,
                canonical_bytes(body).decode(),
                None,
                None,
                None,
            ),
        )
        for name, raw in files.items():
            digest = self.s._blob(tenant, raw)
            self.db.execute(
                "INSERT INTO report_files VALUES (?,?,?,?,?,?,?,?)",
                (tenant, project, args["report"], revision, name, digest, len(raw), "input"),
            )
        self.db.execute(
            "DELETE FROM report_uploads WHERE tenant=? AND project=? AND actor=? AND incarnation=? AND upload=?",
            key,
        )
        return {"report": args["report"], "revision": revision, "state": "draft"}

    def _target(self, who: dict[str, Any], args: dict[str, Any], at: int) -> Any:
        row = self.db.execute(
            "SELECT * FROM report_revisions WHERE tenant=? AND project=? AND report=? AND revision=?",
            (who["tenant"], args["project"], args["report"], args["revision"]),
        ).fetchone()
        if row is None or row["body"] is None or row["state"] == "withdrawn":
            raise LocalError("unavailable")
        if row["incarnation"] != self.s.incarnation:
            raise LocalError("history_changed")
        body = json.loads(row["body"])
        self._allowed(who, body, at)
        if row["state"] != "published" and who["principal"] != row["author"]:
            # QA/publish actors may inspect drafts, ordinary phone readers cannot.
            for cap in ("report_qa", "report_publish"):
                try:
                    self.s._subject_auth(self.s._subject_token(who), cap, row["project"], at)
                    break
                except LocalError:
                    continue
            else:
                raise LocalError("unavailable")
        return row

    def _status(self, row: Any) -> dict[str, Any]:
        body = json.loads(row["body"])
        files = self.db.execute(
            "SELECT name,blob,bytes,kind FROM report_files WHERE tenant=? AND project=? AND report=? AND revision=? ORDER BY name",
            tuple(row[k] for k in ("tenant", "project", "report", "revision")),
        ).fetchall()
        return {
            "report": row["report"],
            "revision": row["revision"],
            "state": row["state"],
            "title": body["title"],
            "technical_status": body["technical_status"],
            "review_status": body["review_status"],
            "source_sha256": body["source_sha256"],
            "readme_sha256": body["readme_sha256"],
            "evidence_sha256": body["evidence_sha256"],
            "artifacts": [
                {"name": f["name"], "sha256": f["blob"], "bytes": f["bytes"]}
                for f in files
                if f["kind"] == "artifact"
            ],
            "qa": json.loads(row["qa"]) if row["qa"] else None,
        }

    def _list(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        epochs = self.db.execute(
            "SELECT policy_epoch,purge_epoch FROM tenants WHERE tenant=?", (who["tenant"],)
        ).fetchone()
        binding = {
            "tenant": who["tenant"],
            "principal": who["principal"],
            "project": args["project"],
            "policy_epoch": epochs[0],
            "purge_epoch": epochs[1],
            "viewer": who.get("viewer"),
            "limit": args["limit"],
        }
        after, revision = "", 0
        if args["cursor"] is not None:
            cursor = self.s._decode(args["cursor"], "report_catalog")
            expected = {
                "kind": "report_catalog",
                "store": self.s.store_id,
                "incarnation": self.s.incarnation,
                **binding,
            }
            if {k: v for k, v in cursor.items() if k not in {"after", "revision"}} != expected:
                raise LocalError("invalid_report_cursor")
            after, revision = cursor["after"], cursor["revision"]
        rows = self.db.execute(
            "SELECT report,revision FROM report_revisions WHERE tenant=? AND project=? AND incarnation=? AND state='published' AND (report>? OR (report=? AND revision>?)) ORDER BY report,revision LIMIT 513",
            (who["tenant"], args["project"], self.s.incarnation, after, after, revision),
        ).fetchall()
        items: list[dict[str, Any]] = []
        more = False
        for candidate in rows[:512]:
            try:
                row = self._target(
                    who,
                    {**args, "report": candidate["report"], "revision": candidate["revision"]},
                    at,
                )
                body = json.loads(row["body"])
                _, policy = self._policy(who["tenant"], args["project"], body["program"])
                if policy["channel"] == "kindle":
                    continue
            except LocalError:
                continue
            if len(items) == args["limit"]:
                more = True
                break
            deliveries = self.db.execute(
                "SELECT state FROM report_deliveries WHERE tenant=? AND project=? AND report=? AND revision=?",
                (who["tenant"], args["project"], row["report"], row["revision"]),
            ).fetchall()
            items.append(
                {
                    "report": row["report"],
                    "revision": row["revision"],
                    "title": body["title"],
                    "program": body["program"],
                    "phase": body["phase"],
                    "task": body["task"],
                    "created_ms": row["created"],
                    "technical_status": body["technical_status"],
                    "review_status": body["review_status"],
                    "channel": policy["channel"],
                    "delivery_states": sorted({d["state"] for d in deliveries}),
                    "device_delivery": "unverified",
                }
            )
            if len(canonical_bytes(items)) > 48 * 1024:
                items.pop()
                more = True
                break
        exhausted = len(rows) > 512 and not more
        if exhausted and not items:
            # Never issue a cursor containing a hidden report identity or claim
            # that an incomplete authorization scan proves an empty catalog.
            raise LocalError("report_catalog_scan_limit")
        more = more or exhausted
        next_cursor = None
        if more:
            last = items[-1]
            next_cursor = self.s._token(
                "report_catalog", **binding, after=last["report"], revision=last["revision"]
            )
        return {
            "reports": items,
            "next_cursor": next_cursor,
            "consistency": "current_authorized_page",
        }

    def _image(self, who: dict[str, Any], row: Any, args: dict[str, Any]) -> dict[str, Any]:
        if row["state"] != "published":
            raise LocalError("unavailable")
        _, policy = self._policy(who["tenant"], row["project"], json.loads(row["body"])["program"])
        if policy["channel"] == "kindle":
            raise LocalError("unavailable")
        if args["name"] not in json.loads(row["body"]).get("figures", []):
            raise LocalError("unavailable")
        return self._artifact(who, row, args, kind="input")

    def _artifact(
        self, who: dict[str, Any], row: Any, args: dict[str, Any], *, kind: str = "artifact"
    ) -> dict[str, Any]:
        file = self.db.execute(
            "SELECT blob,bytes FROM report_files WHERE tenant=? AND project=? AND report=? AND revision=? AND name=? AND kind=?",
            (who["tenant"], row["project"], row["report"], row["revision"], args["name"], kind),
        ).fetchone()
        if file is None or args["offset"] >= file["bytes"]:
            raise LocalError("unavailable")
        raw = self._bytes(who["tenant"], file["blob"], 60 * 1024 * 1024)
        end = min(args["offset"] + 32768, len(raw))
        return {
            "name": args["name"],
            "sha256": file["blob"],
            "bytes": file["bytes"],
            "offset": args["offset"],
            "data_base64": base64.b64encode(raw[args["offset"] : end]).decode(),
            "next_offset": end if end < len(raw) else None,
        }

    def _lifecycle(
        self, who: dict[str, Any], row: Any, action: str, args: dict[str, Any], at: int
    ) -> dict[str, Any]:
        key = tuple(row[k] for k in ("tenant", "project", "report", "revision"))
        body = json.loads(row["body"])
        if action in {"qa", "publish"}:
            files = self.db.execute(
                "SELECT blob,bytes FROM report_files WHERE tenant=? AND project=? AND report=? AND revision=?",
                key,
            ).fetchall()
            for file in files:
                if len(self._bytes(who["tenant"], file["blob"], 60 * 1024 * 1024)) != file["bytes"]:
                    raise LocalError("report_artifact_corrupt")
        if action == "build":
            if row["state"] != "draft" or row["lease_until"] is not None:
                raise LocalError("invalid_report_transition")
            count = self.db.execute(
                "SELECT COUNT(*) FROM report_revisions WHERE tenant=? AND state='draft' AND lease_until=0",
                (who["tenant"],),
            ).fetchone()[0]
            if count >= BUILD_QUEUE_LIMIT:
                raise LocalError("report_queue_full")
            self.db.execute(
                "UPDATE report_revisions SET lease_until=0 WHERE tenant=? AND project=? AND report=? AND revision=?",
                key,
            )
        elif action == "qa":
            if row["state"] != "built":
                raise LocalError("invalid_report_transition")
            qa = args["attestation"]
            expected = {k: body[k] for k in ("source_sha256", "readme_sha256", "evidence_sha256")}
            artifacts = {
                r["name"]: r["blob"]
                for r in self.db.execute(
                    "SELECT name,blob FROM report_files WHERE tenant=? AND project=? AND report=? AND revision=? AND kind='artifact'",
                    key,
                )
            }
            expected.update(
                pdf_sha256=artifacts["report.pdf"],
                epub_sha256=artifacts["report.epub"],
                reader_sha256=artifacts["reader.json"],
            )
            if (
                any(qa[k] != v for k, v in expected.items())
                or qa["report"] != row["report"]
                or qa["revision"] != row["revision"]
                or qa["pdf_pages_reviewed"] != body["pdf_pages"]
            ):
                raise LocalError("report_qa_mismatch")
            record = {**qa, "actor": who["principal"], "evaluated_ms": at, "human_approval": False}
            self.db.execute(
                "UPDATE report_revisions SET state='qa_verified',qa=? WHERE tenant=? AND project=? AND report=? AND revision=?",
                (canonical_bytes(record).decode(), *key),
            )
        elif action == "publish":
            if row["state"] != "qa_verified":
                raise LocalError("report_qa_required")
            version, policy = self._policy(who["tenant"], row["project"], body["program"])
            if version != body["policy_version"]:
                raise LocalError("report_policy_changed")
            for principal in body["readers"]:
                recipient = {"tenant": who["tenant"], "principal": principal}
                self._allowed(recipient, body, at)
            if policy["channel"] != "app":
                from memoryking.adapters.report_delivery import Deliveries

                Deliveries(self.s).enqueue(who, row, policy, version, at)
            self.db.execute(
                "UPDATE report_revisions SET state='published' WHERE tenant=? AND project=? AND report=? AND revision=?",
                key,
            )
            self.s.fault("report_before_publication_commit")
        elif action == "withdraw":
            self.db.execute(
                "UPDATE report_revisions SET state='withdrawn',token=NULL,lease_until=NULL WHERE tenant=? AND project=? AND report=? AND revision=?",
                key,
            )
        else:
            raise LocalError("invalid_report_request")
        return {"report": row["report"], "revision": row["revision"], "action": action}

    def _reader(self, who: dict[str, Any], row: Any, args: dict[str, Any]) -> dict[str, Any]:
        if row["state"] != "published":
            raise LocalError("unavailable")
        _, policy = self._policy(who["tenant"], row["project"], json.loads(row["body"])["program"])
        if policy["channel"] == "kindle":
            raise LocalError("unavailable")
        epochs = self.db.execute(
            "SELECT policy_epoch,purge_epoch FROM tenants WHERE tenant=?", (who["tenant"],)
        ).fetchone()
        binding = {
            "tenant": who["tenant"],
            "principal": who["principal"],
            "project": row["project"],
            "report": row["report"],
            "revision": row["revision"],
            "policy_epoch": epochs[0],
            "purge_epoch": epochs[1],
            "viewer": who.get("viewer"),
        }
        if args["cursor"] is None:
            if args["page"] != 0:
                raise LocalError("invalid_report_cursor")
        elif self.s._decode(args["cursor"], "report_reader") != {
            "kind": "report_reader",
            "store": self.s.store_id,
            "incarnation": self.s.incarnation,
            **binding,
            "page": args["page"],
        }:
            raise LocalError("invalid_report_cursor")
        name = f"page-{args['page']:06d}.json"
        file = self.db.execute(
            "SELECT blob FROM report_files WHERE tenant=? AND project=? AND report=? AND revision=? AND name=? AND kind='reader'",
            (who["tenant"], row["project"], row["report"], row["revision"], name),
        ).fetchone()
        if file is None:
            raise LocalError("unavailable")
        count = json.loads(row["body"])["reader_pages"]
        next_cursor = (
            self.s._token("report_reader", **binding, page=args["page"] + 1)
            if args["page"] + 1 < count
            else None
        )
        return {
            "page": args["page"],
            "pages": count,
            "content": load_strict(self._bytes(who["tenant"], file["blob"], CHUNK_LIMIT)),
            "next_cursor": next_cursor,
        }

    def purge(self, tenant: str) -> None:
        """Conservative tenant-wide derived erasure matches the existing purge contract."""
        self.db.execute("DELETE FROM report_files WHERE tenant=?", (tenant,))
        self.db.execute("DELETE FROM report_uploads WHERE tenant=?", (tenant,))
        self.db.execute("DELETE FROM report_retries WHERE tenant=?", (tenant,))
        self.db.execute(
            "UPDATE report_revisions SET state='withdrawn',body=NULL,qa=NULL,token=NULL,lease_until=NULL WHERE tenant=?",
            (tenant,),
        )
        self.db.execute(
            "UPDATE report_deliveries SET state='cancelled' WHERE tenant=? AND state='queued'",
            (tenant,),
        )
