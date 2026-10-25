"""Bounded legacy import through the existing authorized capture contract.

The source row maps to one private historical observation. Lessons additionally
become unaccepted proposals. No grant, verification, constraint, checkpoint or
active procedure is created by import.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Protocol

from memoryking.adapters.legacy_snapshot import inspect
from memoryking.adapters.local_files import Ownership, atomic, private_dir, private_file
from memoryking.contracts.canonical import canonical_bytes, canonical_hash, load_strict
from memoryking.domain.local import LocalError
from memoryking.sdk import BINDING_FIELDS


class ImportClient(Protocol):
    def context(self) -> dict[str, Any]: ...
    def capture(self, key: str, observation: dict[str, Any]) -> dict[str, Any]: ...
    def read_legacy(self, handle: str) -> dict[str, Any]: ...
    def knowledge(self, command: dict[str, Any]) -> dict[str, Any]: ...


def binding(client: ImportClient) -> dict[str, Any]:
    context = client.context()
    if not {"read", "capture", "admin"} <= set(context["capabilities"]):
        raise LocalError("legacy_operator_required")
    return {key: context[key] for key in BINDING_FIELDS}


def summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        **{key: value for key, value in snapshot.items() if key != "rows"},
        "rows": [
            {key: value for key, value in row.items() if key != "body"} for row in snapshot["rows"]
        ],
    }


def make_plan(path: Path, source: str, profiles: list[str], client: ImportClient) -> dict[str, Any]:
    snapshot = inspect(path, source, expected_profiles=profiles)
    target = binding(client)
    for row in snapshot["rows"]:
        try:
            lesson_text(row)
        except LocalError as exc:
            snapshot["unresolved"].append({"code": exc.code, "key": row["key"]})
    # The plan is small even when the source contains thousands of records.
    # Detailed row mappings live in the independently hashed inspection report.
    return {
        "version": 1,
        "snapshot_sha256": snapshot["snapshot_sha256"],
        "source": source,
        "profiles": profiles,
        "binding": target,
        "rows": len(snapshot["rows"]),
        "tables": snapshot["tables"],
        "unresolved": snapshot["unresolved"],
        "row_set_sha256": hashlib.sha256(
            b"".join(
                canonical_bytes({k: v for k, v in row.items() if k != "body"}) + b"\n"
                for row in snapshot["rows"]
            )
        ).hexdigest(),
        "policy": "private_historical_evidence_and_lesson_proposals_v1",
        "authority": "no_imported_approval_or_activation",
        "retired_policy": "hash_only_tombstone_or_source_conflict",
        "blob_policy": "preserve_in_snapshot_hash_only_in_target",
        "time_policy": "preserve_legacy_values_observed_time_unknown",
    }


def lesson_text(row: dict[str, Any]) -> str | None:
    if row["table"] != "lessons" or row["retired"]:
        return None
    values = row["body"]["values"]
    value = values.get("lesson", values.get("body", {}))
    text = value.get("value") if value.get("type") == "text" else None
    if not isinstance(text, str) or not 1 <= len(text) <= 8192:
        raise LocalError("legacy_lesson_proposal_limit")
    return text


def propose_lesson(
    row: dict[str, Any],
    plan: dict[str, Any],
    request: dict[str, Any],
    receipt: dict[str, Any],
    client: ImportClient,
) -> dict[str, Any] | None:
    text = lesson_text(row)
    if text is None:
        return None
    raw = request["text"].encode()
    key = "legacy_" + canonical_hash(
        {"source": plan["source"], "row": row["key"], "project": plan["binding"]["project"]}
    )
    return client.knowledge(
        {
            "version": 1,
            "incarnation": plan["binding"]["incarnation"],
            "action": "propose",
            "key": key,
            "args": {
                "project": plan["binding"]["project"],
                "logical_key": key,
                "expected_version": 0,
                "preserve_legacy": True,
                "body": {
                    "text": text,
                    "kind": "knowledge",
                    "selector": {"legacy_applicability": "requires_current_review"},
                    "valid_start": None,
                    "valid_end": None,
                    "task": None,
                    "run": None,
                    "readers": request["readers"],
                    "evidence": [
                        {
                            "observation": receipt["observation"],
                            "start_byte": 0,
                            "end_byte": len(raw),
                            "content_hash": hashlib.sha256(raw).hexdigest(),
                        }
                    ],
                    "authority": None,
                    "rule_key": None,
                    "effect": None,
                    "rule_value": None,
                    "provenance": {
                        "migration": "legacy_proposal_requires_current_review",
                        "source": plan["source"],
                        "row_sha256": row["sha256"],
                        "legacy_review_is_not_authority": True,
                        "original_times_not_converted_to_current_validity": True,
                    },
                },
            },
        }
    )


def request_for(row: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    body = row["body"]
    if row["retired"]:
        body = {
            "format": "memoryking-legacy-tombstone-v1",
            "source": plan["source"],
            "table": row["table"],
            "key": row["identity"],
            "source_row_sha256": row["sha256"],
            "authority": "retired_source_content_unavailable",
        }
    else:
        body = {**body, "legacy_reference_keys": row["references"]}
    text = canonical_bytes(body).decode()
    if len(text.encode()) > 65536:
        raise LocalError("legacy_record_too_large")
    return {
        "version": 1,
        "project": plan["binding"]["project"],
        "source": "legacy_" + hashlib.sha256(plan["source"].encode()).hexdigest(),
        "source_event": "row_" + row["key"],
        "text": text,
        "readers": [plan["binding"]["principal"]],
        "classification": "restricted",
        "observed_at_ms": 0,
    }


def apply_batch(
    path: Path,
    plan: dict[str, Any],
    client: ImportClient,
    journal: Path,
    *,
    start: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    if not 1 <= limit <= 500 or start < 0:
        raise LocalError("legacy_batch_limit")
    if plan != make_plan(path, plan["source"], plan["profiles"], client):
        raise LocalError("legacy_plan_changed")
    if plan["unresolved"]:
        raise LocalError("legacy_reconciliation_required")
    snapshot = inspect(path, plan["source"], expected_profiles=plan["profiles"])
    if snapshot["snapshot_sha256"] != plan["snapshot_sha256"]:
        raise LocalError("legacy_plan_changed")
    requests = [request_for(row, plan) for row in snapshot["rows"]]
    for row in snapshot["rows"]:
        lesson_text(row)
    if start > len(requests):
        raise LocalError("legacy_batch_start")
    private_dir(journal, create=True)
    owner = Ownership(journal)
    try:
        identity = journal / "plan.json"
        if identity.exists():
            private_file(identity)
            if load_strict(identity.read_bytes()) != plan:
                raise LocalError("legacy_journal_plan_mismatch")
        else:
            atomic(identity, canonical_bytes(plan))
        completed = 0
        wrote = 0
        mapping: list[dict[str, Any]] = []
        for index, (row, request) in enumerate(zip(snapshot["rows"], requests, strict=True)):
            if index < start:
                continue
            result_path = journal / (row["key"] + ".json")
            if result_path.exists():
                private_file(result_path)
                result = load_strict(result_path.read_bytes())
                if result["request_sha256"] != canonical_hash(request) or result[
                    "plan_sha256"
                ] != canonical_hash(plan):
                    raise LocalError("legacy_journal_corrupt")
                # Repeated import goes through current authorization and stable
                # source identity again. A prior local journal is not authority.
            if wrote == limit:
                break
            if binding(client) != plan["binding"]:
                raise LocalError("legacy_context_changed")
            try:
                receipt = client.capture("import_" + row["key"], request)
                proposal = propose_lesson(row, plan, request, receipt, client)
            except LocalError as exc:
                atomic(
                    journal / "last-failure.json",
                    canonical_bytes(
                        {
                            "row": row["key"],
                            "index": index,
                            "code": exc.code,
                            "automatic_retry": False,
                        }
                    ),
                )
                raise
            result = {
                "row": row["key"],
                "table": row["table"],
                "request_sha256": canonical_hash(request),
                "plan_sha256": canonical_hash(plan),
                "target": receipt["observation"],
                "handle": receipt["handle"],
                "incarnation": receipt["incarnation"],
                "retired": row["retired"],
                "legacy_references": row["references"],
                "proposal": proposal,
            }
            if result["incarnation"] != plan["binding"]["incarnation"]:
                raise LocalError("legacy_context_changed")
            atomic(result_path, canonical_bytes(result))
            mapping.append(result)
            wrote += 1
            completed += 1
        return {
            "version": 1,
            "plan_sha256": canonical_hash(plan),
            "submitted_this_batch": completed,
            "start": start,
            "next_start": start + completed,
            "total_rows": len(requests),
            "end_reached": start + completed == len(requests),
            "mapping": mapping,
            "authority_created": False,
            "live_cutover": False,
            "reconciliation_required": True,
        }
    finally:
        owner.close()


def reconcile(
    path: Path, plan: dict[str, Any], client: ImportClient, journal: Path
) -> dict[str, Any]:
    """Verify every mapped row through current authorized reads, without writes."""
    if plan != make_plan(path, plan["source"], plan["profiles"], client) or plan["unresolved"]:
        raise LocalError("legacy_plan_changed")
    private_dir(journal)
    snapshot = inspect(path, plan["source"], expected_profiles=plan["profiles"])
    if snapshot["snapshot_sha256"] != plan["snapshot_sha256"]:
        raise LocalError("legacy_plan_changed")
    mapping: dict[str, str] = {}
    references = 0
    for row in snapshot["rows"]:
        if binding(client) != plan["binding"]:
            raise LocalError("legacy_context_changed")
        result_path = journal / (row["key"] + ".json")
        if not result_path.exists():
            raise LocalError("legacy_incomplete")
        private_file(result_path)
        result = load_strict(result_path.read_bytes())
        request = request_for(row, plan)
        if (
            result["plan_sha256"] != canonical_hash(plan)
            or result["request_sha256"] != canonical_hash(request)
            or result["incarnation"] != plan["binding"]["incarnation"]
        ):
            raise LocalError("legacy_journal_corrupt")
        items = client.read_legacy(result["handle"])["items"]
        if (
            len(items) != 1
            or any(
                items[0][key] != request[key]
                for key in (
                    "text",
                    "source",
                    "source_event",
                    "project",
                    "classification",
                    "observed_at_ms",
                )
            )
            or items[0]["id"] != result["target"]
        ):
            raise LocalError("legacy_target_mismatch")
        mapping[row["key"]] = result["target"]
        references += len(row["references"])
    if any(ref not in mapping for row in snapshot["rows"] for ref in row["references"]):
        raise LocalError("legacy_reference_missing")
    return {
        "version": 1,
        "plan_sha256": canonical_hash(plan),
        "verified_rows": len(mapping),
        "verified_references": references,
        "mapping": mapping,
        "current_reads_verified": True,
        "point_in_time_read_checks_not_a_global_snapshot": True,
        "authority_created": False,
        "live_cutover": False,
    }
