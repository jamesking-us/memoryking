"""Exact search only after current tenant/project/audience/source eligibility.

The scan is one matrix product over the reader's eligible vectors (numpy), after the storage
query has applied tenant, project, deletion, audience and source freshness. Past
`SCAN_VECTORS` eligible vectors the newest ones are scanned and the result says so; the
channel never answers nothing merely because the corpus is large.
"""

from __future__ import annotations

import json
from typing import Any

from memoryking.application.store_core import StoreCore
from memoryking.contracts.canonical import canonical_hash
from memoryking.domain.embeddings import PACK_SCALE, cosine_ppm, unpack, validate_vector
from memoryking.domain.local import LocalError

# Eligible vectors scanned per query with numpy: 200,000 vectors of 384 dimensions are 154 MB
# of 16-bit integers read from the store and about a quarter of a second of arithmetic. See
# docs/reference.md for the measured latency at this size and the memory it implies.
SCAN_VECTORS = 200_000
# Without numpy (the core install) the scan is the portable pure-Python loop.
SCAN_VECTORS_PYTHON = 10_000
CHUNK = 32768  # rows converted to float32 at a time, which bounds the working memory
SHORTLIST = 20000  # rows whose per-subject best is taken; exact for up to 64 parts a subject
RESULTS = 256

# One statement for both backends: eligibility is inside it, before any limit. The newest
# vectors come first so that a truncated scan keeps recent memory.
ELIGIBLE = (
    "SELECT v.subject,v.vector FROM embedding_vectors v JOIN observations o ON v.tenant=o.tenant AND v.subject=o.id AND v.project=o.project AND v.source_revision=o.revision AND v.blob_hash=o.blob "
    "WHERE v.tenant=? AND v.project=? AND v.fingerprint=? AND v.generation=? AND v.incarnation=? AND v.kind='observation' AND o.deleted=0 AND +o.position<=? "
    "AND (o.readers='null' OR EXISTS(SELECT 1 FROM json_each(o.readers) WHERE value=?)) "
    "ORDER BY v.position DESC LIMIT ?"
)
# Vectors an earlier release wrote as JSON and the indexing loop has not converted yet.
LEGACY = (
    "SELECT i.observation AS subject,i.vector_json FROM embedding_items i JOIN observations o ON i.tenant=o.tenant AND i.observation=o.id AND i.project=o.project AND i.source_revision=o.revision AND i.blob_hash=o.blob "
    "WHERE i.tenant=? AND i.project=? AND i.fingerprint=? AND i.generation=? AND i.incarnation=? AND o.deleted=0 AND +o.position<=? "
    "AND (o.readers='null' OR EXISTS(SELECT 1 FROM json_each(o.readers) WHERE value=?)) "
    "ORDER BY i.position DESC LIMIT ?"
)


def numpy_module() -> Any:
    try:
        import numpy
    except ImportError:
        return None
    return numpy


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
        query_embedding_origin=request.get("origin", "caller_supplied"),
        indexed_kind="observation_passages",
        input_byte_limit=16384,
        input_token_limit=256,
    )
    if args.get("freshness", "any") == "attested":
        result["availability"] = "unsupported_freshness"
        return result
    numpy = numpy_module()
    limit = SCAN_VECTORS if numpy is not None else SCAN_VECTORS_PYTHON
    parameters = (
        who["tenant"],
        args["project"],
        row["fingerprint"],
        row["generation"],
        store.incarnation,
        recorded,
        who["principal"],
        limit + 1,
    )
    minimum = request.get("minimum_similarity_ppm", 200000)
    dimensions = row["dimensions"]
    engine = request.get("engine", "python_exact")
    native = getattr(store, "native_index", None)
    if engine == "pgvector_exact" or (native is not None and numpy is not None):
        version = native() if native is not None else None
        if version is None and engine == "pgvector_exact":
            result["availability"] = "engine_unavailable"
            result["method"] = "pgvector_hnsw_exact_rescore_v1"
            return result
        if version is not None and numpy is not None:
            shortlisted = _hnsw(store, numpy, version, row, request, recorded, who, minimum)
            if shortlisted is not None:
                result.update(shortlisted)
                return result
    rows = store.db.execute(ELIGIBLE, parameters).fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]
    legacy: list[Any] = []
    if len(rows) < limit:
        legacy = store.db.execute(LEGACY, (*parameters[:-1], limit - len(rows) + 1)).fetchall()
        if len(legacy) > limit - len(rows):
            truncated = True
            legacy = legacy[: limit - len(rows)]
    scores: dict[str, int] = {}
    if numpy is not None:
        scores = _scan_numpy(numpy, request["vector"], rows, legacy, dimensions, minimum)
    else:
        for item in rows:
            _keep(
                scores,
                item["subject"],
                cosine_ppm(request["vector"], unpack(item["vector"], dimensions)),
                minimum,
            )
        for item in legacy:
            _keep(
                scores,
                item["subject"],
                cosine_ppm(request["vector"], json.loads(item["vector_json"])),
                minimum,
            )
    ranked = sorted(scores.items(), key=lambda entry: (-entry[1], entry[0]))
    result["scores"] = dict(ranked[:RESULTS])
    result["candidate_limit_reached"] = len(ranked) > RESULTS
    result["eligible_vectors"] = len(rows) + len(legacy)
    result["scan"] = "numpy_matrix_v1" if numpy is not None else "python_loop_v1"
    if truncated:
        # Named, never silent: the newest `limit` eligible vectors were scanned.
        result.update(scan_truncated=True, scan_limit=limit)
    return result


HNSW_SHORTLIST = 1000  # the most pgvector's HNSW search can be asked for in one scan


def _hnsw(
    store: StoreCore,
    numpy: Any,
    version: str,
    row: Any,
    request: dict[str, Any],
    recorded: int,
    who: dict[str, Any],
    minimum: int,
) -> dict[str, Any] | None:
    """The shared-profile approximate path, or None to use the exact scan.

    Only when the operator enabled pgvector (see `enable-vector`) and the partition's vectors
    have the dimensions the index serves. The index shortlists, eligibility is applied to the
    shortlist, and the survivors are scored exactly. When fewer than a full answer survive
    (a reader who can see little of a large partition), the exact, eligibility-first scan
    answers instead, so the approximate index can make a search faster but never emptier."""
    from memoryking.adapters.postgres_admin import NATIVE_DIMENSIONS

    dimensions = row["dimensions"]
    if dimensions != NATIVE_DIMENSIONS:
        return None
    survivors = store.dense_shortlist(  # type: ignore[attr-defined]
        version, row, request["vector"], (who["principal"], recorded), HNSW_SHORTLIST
    )
    scores = _scan_numpy(numpy, request["vector"], survivors, [], dimensions, minimum)
    if len(scores) < RESULTS:
        return None
    ranked = sorted(scores.items(), key=lambda entry: (-entry[1], entry[0]))
    return {
        "scores": dict(ranked[:RESULTS]),
        "candidate_limit_reached": len(ranked) > RESULTS,
        "eligible_vectors": len(survivors),
        "scan": "hnsw_shortlist_exact_rescore_v1",
        "shortlist": HNSW_SHORTLIST,
        "method": f"pgvector_{version.replace('.', '_')}_hnsw_exact_rescore_v1",
    }


def _keep(scores: dict[str, int], subject: str, score: int, minimum: int) -> None:
    if score >= minimum and score > scores.get(subject, -2_000_000):
        scores[subject] = score


def _scan_numpy(
    np: Any,
    query: list[int],
    rows: list[Any],
    legacy: list[Any],
    dimensions: int,
    minimum: int,
) -> dict[str, int]:
    """Cosine of the query against every row, the best part of each subject kept."""
    q = np.asarray(query, dtype=np.float32)
    q /= np.linalg.norm(q)
    subjects = [item["subject"] for item in rows]
    parts = [bytes(item["vector"]) for item in rows]
    for item in legacy:
        subjects.append(item["subject"])
        parts.append(
            np.round(np.asarray(json.loads(item["vector_json"])) * PACK_SCALE / 1_000_000)
            .astype("<i2")
            .tobytes()
        )
    if not subjects:
        return {}
    if any(len(blob) != dimensions * 2 for blob in parts):
        raise LocalError("embedding_vector_invalid")
    matrix = np.frombuffer(b"".join(parts), dtype="<i2").reshape(len(parts), dimensions)
    cosine = np.empty(len(parts), dtype=np.float32)
    for start in range(0, len(parts), CHUNK):
        block = matrix[start : start + CHUNK].astype(np.float32)
        norms = np.sqrt(np.einsum("ij,ij->i", block, block))
        cosine[start : start + CHUNK] = (block @ q) / np.maximum(norms, 1.0)
    ppm = np.rint(cosine.astype(np.float64) * 1_000_000).astype(np.int64)
    keep = np.flatnonzero(ppm >= minimum)
    if len(keep) > SHORTLIST:
        keep = keep[np.argpartition(-ppm[keep], SHORTLIST)[:SHORTLIST]]
    scores: dict[str, int] = {}
    for index in keep.tolist():
        _keep(scores, subjects[index], int(min(1_000_000, ppm[index])), minimum)
    return scores


def claim_scores(
    store: StoreCore,
    who: dict[str, Any],
    args: dict[str, Any],
    revisions: list[str],
    dimensions: int,
) -> dict[str, int]:
    """Similarity of the query to accepted claims the reader is already eligible for.

    The caller passes only revisions its knowledge query returned, so a claim that is not
    visible to the reader, is superseded or is retired never reaches this function; the
    vectors are looked up by those revisions and nothing else."""
    request = args.get("dense")
    if request is None or not revisions:
        return {}
    row = store.db.execute(
        "SELECT * FROM embedding_partitions WHERE tenant=? AND project=? AND fingerprint=?",
        (who["tenant"], args["project"], request["fingerprint"]),
    ).fetchone()
    if row is None or row["state"] != "experimental" or row["incarnation"] != store.incarnation:
        return {}
    scores: dict[str, int] = {}
    numpy = numpy_module()
    for start in range(0, len(revisions), 500):
        group = revisions[start : start + 500]
        found = store.db.execute(
            "SELECT subject,vector FROM embedding_vectors WHERE tenant=? AND project=? AND fingerprint=? AND generation=? AND incarnation=? AND kind='claim' AND subject IN ("
            + ",".join("?" for _ in group)
            + ")",
            (
                who["tenant"],
                args["project"],
                row["fingerprint"],
                row["generation"],
                store.incarnation,
                *group,
            ),
        ).fetchall()
        if numpy is not None:
            scores.update(
                _scan_numpy(numpy, request["vector"], found, [], row["dimensions"], 200000)
            )
        else:
            for item in found:
                _keep(
                    scores,
                    item["subject"],
                    cosine_ppm(request["vector"], unpack(item["vector"], dimensions)),
                    200000,
                )
    return scores
