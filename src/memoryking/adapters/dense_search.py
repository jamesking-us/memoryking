"""Exact search only after current tenant/project/audience/source eligibility."""

from __future__ import annotations

import json
from typing import Any

from memoryking.application.store_core import StoreCore
from memoryking.contracts.canonical import canonical_hash
from memoryking.domain.embeddings import cosine_ppm, validate_vector
from memoryking.domain.local import LocalError

SCAN_LIMIT = 10000


def search(
    store: StoreCore, who: dict[str, Any], args: dict[str, Any], recorded: int, target: int
) -> dict[str, Any]:
    request = args.get("dense")
    result: dict[str, Any] = {
        "availability": "unavailable",
        "position": None,
        "scores": {},
        "method": "authorized_exact_cosine_v1",
        "candidate_limit_reached": False,
    }
    if request is None:
        return result
    if request["query_hash"] != canonical_hash(args["query"]):
        raise LocalError("embedding_query_mismatch")
    row = store.db.execute(
        "SELECT * FROM embedding_partitions WHERE tenant=? AND project=? AND fingerprint=?",
        (who["tenant"], args["project"], request["fingerprint"]),
    ).fetchone()
    if row is None or row["state"] != "experimental" or row["incarnation"] != store.incarnation:
        return result
    validate_vector(request["vector"], row["dimensions"])
    result.update(
        position=row["frontier"],
        fingerprint=row["fingerprint"],
        generation=row["generation"],
        availability="ready" if row["frontier"] >= target else "indexing",
        query_embedding_origin="caller_supplied",
        indexed_kind="observation_prefix",
        input_byte_limit=16384,
        input_token_limit=256,
    )
    if args.get("freshness", "any") == "attested":
        result["availability"] = "unsupported_freshness"
        return result
    # Eligibility is inside the storage query, before the scan or candidate cap.
    # An old or purged revision cannot be resurrected by an index entry.
    statement = "SELECT i.observation,i.vector_json FROM embedding_items i JOIN observations o ON i.tenant=o.tenant AND i.observation=o.id AND i.project=o.project AND i.source_revision=o.revision AND i.blob_hash=o.blob WHERE i.tenant=? AND i.project=? AND i.fingerprint=? AND i.generation=? AND i.incarnation=? AND o.deleted=0 AND o.position<=? AND (o.readers='null' OR EXISTS(SELECT 1 FROM json_each(o.readers) WHERE value=?)) ORDER BY i.observation LIMIT ?"
    parameters = (
        who["tenant"],
        args["project"],
        row["fingerprint"],
        row["generation"],
        store.incarnation,
        recorded,
        who["principal"],
        SCAN_LIMIT + 1,
    )
    if request.get("engine", "python_exact") == "pgvector_exact":
        native = getattr(store, "dense_exact", None)
        scored = native(statement, parameters, request, SCAN_LIMIT) if native else None
        result["method"] = "pgvector_0_8_7_exact_cosine_v1"
        if scored is None:
            result["availability"] = "engine_unavailable"
        else:
            result.update(scored)
        return result
    rows = store.db.execute(statement, parameters).fetchall()
    if len(rows) > SCAN_LIMIT:
        result.update(availability="scan_limit", candidate_limit_reached=True)
        return result
    ranked = []
    for item in rows:
        score = cosine_ppm(request["vector"], json.loads(item["vector_json"]))
        if score >= request.get("minimum_similarity_ppm", 200000):
            ranked.append((score, item["observation"]))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    result["scores"] = {reference: score for score, reference in ranked[:256]}
    result["candidate_limit_reached"] = len(ranked) > 256
    result["eligible_vectors"] = len(rows)
    return result
