"""Durable, opt-in embedding projection jobs under the existing release gate."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from memoryking.adapters.local_embeddings import SUPPORTED, model_manifest
from memoryking.adapters.local_store import MAX_COUNTER, RETRY_MS, USAGE_LIMIT, uid
from memoryking.adapters.retrieval import Retrieval
from memoryking.application.records import partition_tables
from memoryking.application.retention import index_receipt
from memoryking.application.store_core import StoreCore
from memoryking.contracts.canonical import canonical_bytes, canonical_hash
from memoryking.domain.embeddings import EmbeddingBatch, fingerprint, pack
from memoryking.domain.local import LocalError, Released
from memoryking.domain.passages import split

# Embed each observation passage by passage (`domain.passages`) rather than from its opening.
# Off: on the pooled LongMemEval oracle set passage vectors lowered four of the five headline
# metrics against the opening 512 tokens (docs/reference.md), so the default embeds the
# opening. The passage path is complete and tested; this is the one switch.
EMBED_PASSAGES = False

OPENING_BYTES = 16384  # the most of a record that one input holds
INPUT_BYTES = 131072  # the most text one model call holds (`domain.embeddings.validate_inputs`)
LEASE_LIMIT = 4  # live job leases one tenant may hold when claiming one job at a time
BATCH_LEASE_LIMIT = 64  # ... and when claiming in batches
VALIDATOR = Draft202012Validator(
    json.loads(Path(__file__).with_name("embedding.schema.json").read_text(encoding="utf-8"))
)


def receipt_observations(result: dict[str, Any]) -> list[str]:
    """The observations a response delivered text of or stored a vector for, for its receipt."""
    found = [result["observation"]] if "observation" in result else []
    for item in [*result.get("jobs", []), *result.get("results", [])]:
        if "observation" in item:
            found.append(item["observation"])
    return found


class Embeddings:
    def __init__(self, store: StoreCore):
        self.s, self.db, self.r = store, store.db, Retrieval(store)

    def execute(self, credential: str, command: dict[str, Any]) -> Released:
        if len(canonical_bytes(command)) > 131072 or not VALIDATOR.is_valid(command):
            raise LocalError("invalid_request")
        if command["incarnation"] != self.s.incarnation:
            raise LocalError("history_changed")
        action, args = command["action"], command["args"]
        receipt = uid("rcpt")
        with self.s.transaction() as decision:
            at = decision.evaluated_ms
            who = self.s._auth(
                credential, "admin" if action == "configure" else "jobs", args["project"], at
            )
            self.s._auth(credential, "read", args["project"], at)
            if action == "configure":
                result = self.configure(who, command, at)
            elif action == "claim":
                result = self.claim(who, args, at)
            elif action == "claim_batch":
                result = self.claim_batch(who, args, at)
            elif action == "complete_batch":
                result = self.finish_batch(who, args, at)
                self.note_rate(sum(1 for item in result["results"] if item["status"] == "complete"))
            elif action in {"complete", "fail"}:
                result = self.finish(who, args, action == "complete", at)
                if action == "complete":
                    self.note_rate(1)
            else:
                part = self.partition(who, args)
                result = self.status(part)
            epochs = self.db.execute(
                "SELECT policy_epoch,purge_epoch FROM tenants WHERE tenant=?", (who["tenant"],)
            ).fetchone()
            result.update(
                receipt=receipt,
                evaluated_ms=at,
                deadline_ms=decision.deadline_ms,
                incarnation=self.s.incarnation,
            )
            raw = canonical_bytes(result)
            self.db.execute(
                "INSERT INTO receipts(tenant,id,principal,incarnation,evaluated,deadline,policy_epoch,purge_epoch,response_hash,observation_ids) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    who["tenant"],
                    receipt,
                    who["principal"],
                    self.s.incarnation,
                    at,
                    decision.deadline_ms,
                    *epochs,
                    hashlib.sha256(raw).hexdigest(),
                    json.dumps(receipt_observations(result)),
                ),
            )
            index_receipt(self.db, who["tenant"], receipt)
            self.s.fault("after_validation")
        self.s._expose()
        return Released(raw, decision, self.s.clock, receipt)

    def partition(self, who: dict[str, Any], args: dict[str, Any]) -> Any:
        row = self.db.execute(
            "SELECT * FROM embedding_partitions WHERE tenant=? AND project=? AND fingerprint=?",
            (who["tenant"], args["project"], args["fingerprint"]),
        ).fetchone()
        if row is None:
            raise LocalError("embedding_partition_unavailable")
        if row["incarnation"] != self.s.incarnation:
            raise LocalError("history_changed")
        return row

    def status(self, row: Any) -> dict[str, Any]:
        source = self.db.execute(
            "SELECT position FROM tenants WHERE tenant=?", (row["tenant"],)
        ).fetchone()[0]
        return {
            "fingerprint": row["fingerprint"],
            "dimensions": row["dimensions"],
            "generation": row["generation"],
            "state": row["state"],
            "source_position": source,
            "scheduled_position": row["scheduled"],
            "frontier": row["frontier"],
            "ready": row["state"] == "experimental" and row["frontier"] >= source,
            "activation": "explicit_experiment_only",
            "indexed_kind": "observation_prefix",
            "input_byte_limit": 16384,
            "input_token_limit": 256,
        }

    def configure(self, who: dict[str, Any], command: dict[str, Any], at: int) -> dict[str, Any]:
        args = command["args"]
        if args["model"] not in SUPPORTED:
            raise LocalError("embedding_model_unsupported")
        lookup = (
            who["tenant"],
            self.s.incarnation,
            "embedding-config-v1",
            who["principal"],
            "configure",
            command["key"],
        )
        previous = self.db.execute(
            "SELECT * FROM retries WHERE tenant=? AND incarnation=? AND namespace=? AND principal=? AND operation=? AND key=?",
            lookup,
        ).fetchone()
        if previous:
            if previous["hash"] != canonical_hash(command):
                raise LocalError("idempotency_conflict")
            if previous["result"] is None or at >= previous["expires"]:
                raise LocalError("idempotency_result_expired")
            return {**json.loads(previous["result"]), "replayed": True}
        configuration = model_manifest(args["model"])
        fp = fingerprint(configuration)
        key = (who["tenant"], args["project"], fp)
        row = self.db.execute(
            "SELECT * FROM embedding_partitions WHERE tenant=? AND project=? AND fingerprint=?", key
        ).fetchone()
        generation = row["generation"] if row else 0
        if args["expected_generation"] != generation:
            raise LocalError("embedding_generation_conflict")
        if generation >= MAX_COUNTER:
            raise LocalError("counter_exhausted")
        if (
            row is None
            and self.db.execute(
                "SELECT count(*) FROM embedding_partitions WHERE tenant=?", (who["tenant"],)
            ).fetchone()[0]
            >= 16
        ):
            raise LocalError("embedding_partition_limit")
        for table in partition_tables():
            self.db.execute(
                f"DELETE FROM {table} WHERE tenant=? AND project=? AND fingerprint=?", key
            )
        self.db.execute(
            "INSERT INTO embedding_partitions(tenant,project,fingerprint,incarnation,generation,dimensions,configuration,state,scheduled,frontier,created) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(tenant,project,fingerprint) DO UPDATE SET incarnation=excluded.incarnation,generation=excluded.generation,configuration=excluded.configuration,state=excluded.state,scheduled=0,frontier=0",
            (
                *key,
                self.s.incarnation,
                generation + 1,
                configuration["dimensions"],
                json.dumps(configuration),
                args["state"],
                0,
                0,
                at,
            ),
        )
        position = self.s._event(who["tenant"], "embedding_configure", fp, at, who["principal"])
        self.s._enqueue(who["tenant"], position, None, who["principal"])
        result = {
            "fingerprint": fp,
            "dimensions": configuration["dimensions"],
            "generation": generation + 1,
            "state": args["state"],
            "source_position": position,
            "replayed": False,
        }
        self.db.execute(
            "INSERT INTO retries(tenant,incarnation,namespace,principal,operation,key,hash,result,observation,expires) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (*lookup, canonical_hash(command), json.dumps(result), None, at + RETRY_MS),
        )
        return result

    def schedule(self, part: Any, at: int) -> None:
        key = (part["tenant"], part["project"], part["fingerprint"], part["generation"])
        pending = self.db.execute(
            "SELECT count(*) FROM embedding_jobs WHERE tenant=? AND project=? AND fingerprint=? AND generation=? AND state NOT IN ('complete','noop')",
            key,
        ).fetchone()[0]
        room = max(0, min(256, 1000 - pending))
        rows = self.db.execute(
            "SELECT e.position,o.id,o.revision,o.blob,o.deleted FROM events e LEFT JOIN observations o ON e.tenant=o.tenant AND e.subject=o.id AND o.project=? WHERE e.tenant=? AND e.position>? ORDER BY e.position LIMIT ?",
            (part["project"], part["tenant"], part["scheduled"], room),
        ).fetchall()
        epoch = self.db.execute(
            "SELECT purge_epoch FROM tenants WHERE tenant=?", (part["tenant"],)
        ).fetchone()[0]
        for row in rows:
            observation = row["id"] if row["id"] and not row["deleted"] else None
            self.db.execute(
                "INSERT INTO embedding_jobs(tenant,project,fingerprint,generation,position,observation,source_revision,blob_hash,incarnation,state,attempts,fence,lease_until,available,principal,purge_epoch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    *key,
                    row["position"],
                    observation,
                    row["revision"] if observation else 0,
                    row["blob"] if observation else None,
                    self.s.incarnation,
                    "pending" if observation else "noop",
                    0,
                    0,
                    0,
                    at,
                    None,
                    epoch,
                ),
            )
        if rows:
            self.db.execute(
                "UPDATE embedding_partitions SET scheduled=? WHERE tenant=? AND project=? AND fingerprint=?",
                (rows[-1]["position"], *key[:3]),
            )
        self.advance(part)

    def advance(self, part: Any) -> None:
        key = (part["tenant"], part["project"], part["fingerprint"], part["generation"])
        fresh = self.db.execute(
            "SELECT frontier,scheduled FROM embedding_partitions WHERE tenant=? AND project=? AND fingerprint=?",
            key[:3],
        ).fetchone()
        frontier = fresh["frontier"]
        rows = self.db.execute(
            "SELECT position,state FROM embedding_jobs WHERE tenant=? AND project=? AND fingerprint=? AND generation=? AND position>? ORDER BY position LIMIT 1256",
            (*key, frontier),
        ).fetchall()
        for row in rows:
            if row["position"] != frontier + 1 or row["state"] not in {"complete", "noop"}:
                break
            frontier += 1
        self.db.execute(
            "UPDATE embedding_partitions SET frontier=? WHERE tenant=? AND project=? AND fingerprint=?",
            (frontier, *key[:3]),
        )
        self.db.execute(
            "DELETE FROM embedding_jobs WHERE tenant=? AND project=? AND fingerprint=? AND generation=? AND position<?",
            (*key, max(0, frontier - 256)),
        )

    def convert_legacy(self, part: Any, batch: int = 500) -> int:
        """Move vectors that an earlier release stored as JSON into the binary table.

        A bounded batch each time the indexing loop asks for work, so an upgraded store
        converts itself over a few passes and answers searches throughout: the search reads
        both tables until the old one is empty."""
        key = (part["tenant"], part["project"], part["fingerprint"], part["generation"])
        rows = self.db.execute(
            "SELECT * FROM embedding_items WHERE tenant=? AND project=? AND fingerprint=? AND generation=? ORDER BY observation LIMIT ?",
            (*key, batch),
        ).fetchall()
        for row in rows:
            self.db.execute(
                "INSERT INTO embedding_vectors(tenant,project,fingerprint,generation,kind,subject,part,incarnation,source_revision,blob_hash,input_hash,vector,start_byte,end_byte,input_tokens,truncated,position) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(tenant,project,fingerprint,generation,kind,subject,part) DO NOTHING",
                (
                    *key,
                    "observation",
                    row["observation"],
                    0,
                    row["incarnation"],
                    row["source_revision"],
                    row["blob_hash"],
                    row["input_hash"],
                    pack(json.loads(row["vector_json"])),
                    row["start_byte"],
                    row["end_byte"],
                    row["input_tokens"],
                    1,  # later passages, if there are any, are embedded next
                    row["position"],
                ),
            )
            self.db.execute(
                "DELETE FROM embedding_items WHERE tenant=? AND project=? AND fingerprint=? AND generation=? AND observation=?",
                (*key, row["observation"]),
            )
        return len(rows)

    def claim_text(self, who: dict[str, Any], project: str, revision: str, at: int) -> str | None:
        """The text of an accepted-or-proposed claim revision this worker may read, or None."""
        try:
            row = self.r.k._revision(who["tenant"], revision, project)
        except LocalError:
            return None
        if not self.r.k._available(who, row, at):
            return None
        text = json.loads(row["body"]).get("text")
        return text[:16384] if isinstance(text, str) and text.strip() else None

    def claim_revision(
        self, who: dict[str, Any], part: Any, args: dict[str, Any], at: int
    ) -> dict[str, Any] | None:
        """Hand out the oldest claim revision of the project that has no vector yet.

        Claims are embedded after the observations, so reviewed knowledge can be found by
        meaning. Only text the worker may read is released, and a vector is stored only for
        the exact text that was released (the lease names its hash)."""
        rows = self.db.execute(
            "SELECT r.id FROM revisions r JOIN families f ON r.tenant=f.tenant AND r.family=f.id WHERE r.tenant=? AND f.project=? AND r.purged=0 AND r.body IS NOT NULL "
            "AND NOT EXISTS(SELECT 1 FROM embedding_vectors v WHERE v.tenant=r.tenant AND v.project=f.project AND v.fingerprint=? AND v.generation=? AND v.kind='claim' AND v.subject=r.id) "
            "AND NOT EXISTS(SELECT 1 FROM embedding_skips k WHERE k.tenant=r.tenant AND k.fingerprint=? AND k.generation=? AND k.subject=r.id) ORDER BY r.created,r.id LIMIT 32",
            (
                who["tenant"],
                args["project"],
                part["fingerprint"],
                part["generation"],
                part["fingerprint"],
                part["generation"],
            ),
        ).fetchall()
        for row in rows:
            text = self.claim_text(who, args["project"], row["id"], at)
            if text is None:
                continue
            attempt = uid("usage")
            self.db.execute(
                "INSERT INTO processing_usage(tenant,id,project,principal,incarnation,provider,fingerprint,stage,status,input_tokens,cached_input_tokens,output_tokens,cost_microusd,duration_ms,request_hash,output_hash,dependencies,created,expires) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    who["tenant"],
                    attempt,
                    args["project"],
                    who["principal"],
                    self.s.incarnation,
                    "local-onnx",
                    part["fingerprint"],
                    "embedding_backfill",
                    "started",
                    None,
                    0,
                    0,
                    0,
                    0,
                    canonical_hash(text),
                    None,
                    json.dumps([{"kind": "claim", "id": row["id"]}]),
                    at,
                    at + RETRY_MS,
                ),
            )
            lease = self.s._token(
                "embedding_lease",
                tenant=who["tenant"],
                principal=who["principal"],
                project=args["project"],
                fingerprint=part["fingerprint"],
                generation=part["generation"],
                position=0,
                fence=0,
                attempt=attempt,
                input_hash=canonical_hash(text),
                subject_kind="claim",
                subject=row["id"],
            )
            return {
                "job": 0,
                "kind": "claim",
                "lease": lease,
                "lease_until_ms": at + 30000,
                "input": text,
                "input_hash": canonical_hash(text),
                "fingerprint": part["fingerprint"],
                "dimensions": part["dimensions"],
                "observation": row["id"],
                "blob_hash": None,
                "start_byte": 0,
                "end_byte": len(text.encode()),
                "egress": "local_cpu_only",
            }
        return None

    def finish_claim(
        self,
        who: dict[str, Any],
        args: dict[str, Any],
        token: dict[str, Any],
        part: Any,
        complete: bool,
        at: int,
    ) -> dict[str, Any]:
        revision = token["subject"]
        if part["generation"] != token["generation"] or part["state"] != "experimental":
            raise LocalError("stale_lease")
        text = self.claim_text(who, args["project"], revision, at)
        if text is None or canonical_hash(text) != token["input_hash"]:
            raise LocalError("stale_source")
        output_hash = None
        if complete:
            batch = EmbeddingBatch(
                args["fingerprint"],
                part["dimensions"],
                [args["vector"]],
                [args["input_hash"]],
                args["input_tokens"],
                [args["truncated"]],
            )
            batch.validate(token["fingerprint"], part["dimensions"], [text])
            output_hash = canonical_hash(args["vector"])
            row = self.r.k._revision(who["tenant"], revision, args["project"])
            self.db.execute(
                "INSERT INTO embedding_vectors(tenant,project,fingerprint,generation,kind,subject,part,incarnation,source_revision,blob_hash,input_hash,vector,start_byte,end_byte,input_tokens,truncated,position) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(tenant,project,fingerprint,generation,kind,subject,part) DO UPDATE SET incarnation=excluded.incarnation,source_revision=excluded.source_revision,blob_hash=excluded.blob_hash,input_hash=excluded.input_hash,vector=excluded.vector,start_byte=excluded.start_byte,end_byte=excluded.end_byte,input_tokens=excluded.input_tokens,truncated=excluded.truncated,position=excluded.position",
                (
                    who["tenant"],
                    args["project"],
                    token["fingerprint"],
                    token["generation"],
                    "claim",
                    revision,
                    0,
                    self.s.incarnation,
                    0,
                    row["body_hash"],
                    args["input_hash"],
                    pack(args["vector"]),
                    0,
                    len(text.encode()),
                    args["input_tokens"],
                    int(args["truncated"]),
                    row["created"],
                ),
            )
        else:
            # A claim that cannot be embedded is skipped for this generation, not retried
            # on every pass.
            self.db.execute(
                "INSERT INTO embedding_skips(tenant,fingerprint,generation,subject) VALUES (?,?,?,?) ON CONFLICT(tenant,fingerprint,generation,subject) DO NOTHING",
                (who["tenant"], token["fingerprint"], token["generation"], revision),
            )
        self.db.execute(
            "UPDATE processing_usage SET status=?,input_tokens=?,duration_ms=?,output_hash=? WHERE tenant=? AND id=? AND principal=?",
            (
                "succeeded" if complete else "failed",
                args["input_tokens"],
                args["duration_ms"],
                output_hash,
                who["tenant"],
                token["attempt"],
                who["principal"],
            ),
        )
        return {
            "status": "complete" if complete else "dead",
            "fingerprint": token["fingerprint"],
            "observation": revision,
            "usage_attempt": token["attempt"],
        }

    def source(
        self, who: dict[str, Any], row: Any, at: int, number: int = 0
    ) -> tuple[str, int, int, bool]:
        """Passage `number` of the observation's text: (text, start byte, end byte, more).

        The observation stays the unit of evidence and authorization: it must be visible
        to the worker and unchanged since the job was planned; a passage is only a view of
        it, cut by the same rule the lexical index and the briefing's quotes use."""
        observation = self.db.execute(
            "SELECT * FROM observations WHERE tenant=? AND id=? AND project=?",
            (who["tenant"], row["observation"], row["project"]),
        ).fetchone()
        if observation is None or not self.s._visible(who, observation, at):
            raise LocalError("embedding_source_unavailable")
        if (
            observation["revision"] != row["source_revision"]
            or observation["blob"] != row["blob_hash"]
        ):
            raise LocalError("stale_source")
        full = self.r._blob(observation)[:16384].decode("utf-8", errors="ignore")
        if not EMBED_PASSAGES:
            if number:
                raise LocalError("stale_source")
            return full, 0, len(full.encode()), False
        passages = split(full)
        if number >= len(passages):
            raise LocalError("stale_source")
        piece = passages[number]
        prefix = len(full[: piece.start].encode())
        return (
            full[piece.start : piece.end],
            prefix,
            prefix + len(full[piece.start : piece.end].encode()),
            number + 1 < len(passages),
        )

    def claim_passage(
        self, who: dict[str, Any], part: Any, args: dict[str, Any], at: int
    ) -> dict[str, Any] | None:
        """Hand out the next passage of an observation whose earlier passages are embedded.

        A vector row marked `truncated` has more passages after it; the next one is offered
        until the last, so a long capture is found by any of its passages."""
        rows = self.db.execute(
            "SELECT v.* FROM embedding_vectors v WHERE v.tenant=? AND v.project=? AND v.fingerprint=? AND v.generation=? AND v.kind='observation' AND v.truncated=1 "
            "AND NOT EXISTS(SELECT 1 FROM embedding_vectors n WHERE n.tenant=v.tenant AND n.project=v.project AND n.fingerprint=v.fingerprint AND n.generation=v.generation AND n.kind=v.kind AND n.subject=v.subject AND n.part=v.part+1) "
            "ORDER BY v.position,v.subject LIMIT 16",
            (who["tenant"], args["project"], part["fingerprint"], part["generation"]),
        ).fetchall()
        for row in rows:
            job = {
                "observation": row["subject"],
                "project": args["project"],
                "source_revision": row["source_revision"],
                "blob_hash": row["blob_hash"],
            }
            try:
                text, start, end, _ = self.source(who, job, at, row["part"] + 1)
            except LocalError:
                # No further passage (or the source moved on): stop offering this one.
                self.db.execute(
                    "UPDATE embedding_vectors SET truncated=0 WHERE tenant=? AND project=? AND fingerprint=? AND generation=? AND kind='observation' AND subject=? AND part=?",
                    (
                        who["tenant"],
                        args["project"],
                        part["fingerprint"],
                        part["generation"],
                        row["subject"],
                        row["part"],
                    ),
                )
                continue
            attempt = uid("usage")
            self.db.execute(
                "INSERT INTO processing_usage(tenant,id,project,principal,incarnation,provider,fingerprint,stage,status,input_tokens,cached_input_tokens,output_tokens,cost_microusd,duration_ms,request_hash,output_hash,dependencies,created,expires) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    who["tenant"],
                    attempt,
                    args["project"],
                    who["principal"],
                    self.s.incarnation,
                    "local-onnx",
                    part["fingerprint"],
                    "embedding_backfill",
                    "started",
                    None,
                    0,
                    0,
                    0,
                    0,
                    canonical_hash(text),
                    None,
                    json.dumps([{"kind": "observation", "id": row["subject"]}]),
                    at,
                    at + RETRY_MS,
                ),
            )
            lease = self.s._token(
                "embedding_lease",
                tenant=who["tenant"],
                principal=who["principal"],
                project=args["project"],
                fingerprint=part["fingerprint"],
                generation=part["generation"],
                position=0,
                fence=0,
                attempt=attempt,
                input_hash=canonical_hash(text),
                subject_kind="passage",
                subject=row["subject"],
                part=row["part"] + 1,
                source_revision=row["source_revision"],
                blob_hash=row["blob_hash"],
                vector_position=row["position"],
            )
            return {
                "job": 0,
                "kind": "passage",
                "lease": lease,
                "lease_until_ms": at + 30000,
                "input": text,
                "input_hash": canonical_hash(text),
                "fingerprint": part["fingerprint"],
                "dimensions": part["dimensions"],
                "observation": row["subject"],
                "blob_hash": row["blob_hash"],
                "start_byte": start,
                "end_byte": end,
                "egress": "local_cpu_only",
            }
        return None

    def finish_passage(
        self,
        who: dict[str, Any],
        args: dict[str, Any],
        token: dict[str, Any],
        part: Any,
        complete: bool,
        at: int,
    ) -> dict[str, Any]:
        if part["generation"] != token["generation"] or part["state"] != "experimental":
            raise LocalError("stale_lease")
        job = {
            "observation": token["subject"],
            "project": args["project"],
            "source_revision": token["source_revision"],
            "blob_hash": token["blob_hash"],
        }
        text, start, end, more = self.source(who, job, at, token["part"])
        if canonical_hash(text) != token["input_hash"]:
            raise LocalError("stale_source")
        output_hash = None
        if complete:
            batch = EmbeddingBatch(
                args["fingerprint"],
                part["dimensions"],
                [args["vector"]],
                [args["input_hash"]],
                args["input_tokens"],
                [args["truncated"]],
            )
            batch.validate(token["fingerprint"], part["dimensions"], [text])
            output_hash = canonical_hash(args["vector"])
            self.write_vector(
                who,
                token,
                args,
                "observation",
                token["subject"],
                token["part"],
                token["source_revision"],
                token["blob_hash"],
                start,
                end,
                more,
                token["vector_position"],
            )
            # The previous passage no longer has a successor to wait for.
            self.db.execute(
                "UPDATE embedding_vectors SET truncated=0 WHERE tenant=? AND project=? AND fingerprint=? AND generation=? AND kind='observation' AND subject=? AND part=?",
                (
                    who["tenant"],
                    args["project"],
                    token["fingerprint"],
                    token["generation"],
                    token["subject"],
                    token["part"] - 1,
                ),
            )
        self.db.execute(
            "UPDATE processing_usage SET status=?,input_tokens=?,duration_ms=?,output_hash=? WHERE tenant=? AND id=? AND principal=?",
            (
                "succeeded" if complete else "failed",
                args["input_tokens"],
                args["duration_ms"],
                output_hash,
                who["tenant"],
                token["attempt"],
                who["principal"],
            ),
        )
        return {
            "status": "complete" if complete else "dead",
            "fingerprint": token["fingerprint"],
            "observation": token["subject"],
            "usage_attempt": token["attempt"],
        }

    def write_vector(
        self,
        who: dict[str, Any],
        token: dict[str, Any],
        args: dict[str, Any],
        kind: str,
        subject: str,
        number: int,
        revision: int,
        blob: str | None,
        start: int,
        end: int,
        more: bool,
        position: int,
    ) -> None:
        self.db.execute(
            "INSERT INTO embedding_vectors(tenant,project,fingerprint,generation,kind,subject,part,incarnation,source_revision,blob_hash,input_hash,vector,start_byte,end_byte,input_tokens,truncated,position) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(tenant,project,fingerprint,generation,kind,subject,part) DO UPDATE SET incarnation=excluded.incarnation,source_revision=excluded.source_revision,blob_hash=excluded.blob_hash,input_hash=excluded.input_hash,vector=excluded.vector,start_byte=excluded.start_byte,end_byte=excluded.end_byte,input_tokens=excluded.input_tokens,truncated=excluded.truncated,position=excluded.position",
            (
                who["tenant"],
                args["project"],
                token["fingerprint"],
                token["generation"],
                kind,
                subject,
                number,
                self.s.incarnation,
                revision,
                blob,
                args["input_hash"],
                pack(args["vector"]),
                start,
                end,
                args["input_tokens"],
                int(more),  # for an observation: more passages follow this one
                position,
            ),
        )

    def prepare(self, who: dict[str, Any], args: dict[str, Any], at: int) -> Any:
        """The partition, scheduled up to date, with expired exhausted leases retired."""
        part = self.partition(who, args)
        if part["state"] != "experimental":
            raise LocalError("embedding_disabled")
        self.schedule(part, at)
        self.convert_legacy(part)
        self.db.execute(
            "UPDATE embedding_jobs SET state='dead',fence=fence+1 WHERE tenant=? AND state='leased' AND lease_until<=? AND attempts>=5",
            (who["tenant"], at),
        )
        return part

    def claim(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        part = self.prepare(who, args, at)
        if self.live_leases(who, at) >= LEASE_LIMIT:
            raise LocalError("concurrency_limit")
        return self.claim_one(who, part, args, at)

    def claim_batch(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        """Up to `count` jobs in one call, each with its own lease, usage attempt and fence.

        Every job is claimed exactly as `claim` would hand it out; the batch only saves the
        round trips. Fewer than asked for are returned when the queue or the lease limit runs
        out, and none (with the status of `claim`) when there is nothing to do."""
        part = self.prepare(who, args, at)
        if self.live_leases(who, at) >= BATCH_LEASE_LIMIT:
            raise LocalError("concurrency_limit")
        jobs: list[dict[str, Any]] = []
        # The model takes at most INPUT_BYTES of text in one call (and 16 texts), so a batch is
        # closed once another record of up to OPENING_BYTES could no longer fit.
        size = 0
        while (
            len(jobs) < args["count"]
            and size + OPENING_BYTES <= INPUT_BYTES
            and self.live_leases(who, at) < BATCH_LEASE_LIMIT
        ):
            job = self.claim_one(who, part, args, at)
            if job["job"] is None:
                if not jobs:
                    return {"jobs": [], **job}
                break
            jobs.append(job)
            size += len(job["input"].encode())
        return {"jobs": jobs, "job": jobs[0]["job"] if jobs else None}

    def note_rate(self, count: int) -> None:
        """Records a second over the time since the previous completion, smoothed. A pause
        of a minute or more is not indexing speed and starts the measure afresh."""
        now = time.monotonic()
        last, self.s._embedding_mark = self.s._embedding_mark, now
        if last is None or count <= 0 or not 0 < now - last < 60:
            return
        sample, before = count / (now - last), self.s._embedding_rate
        self.s._embedding_rate = sample if before is None else 0.7 * before + 0.3 * sample

    def live_leases(self, who: dict[str, Any], at: int) -> int:
        return int(
            self.db.execute(
                "SELECT count(*) FROM embedding_jobs WHERE tenant=? AND state='leased' AND lease_until>?",
                (who["tenant"], at),
            ).fetchone()[0]
        )

    def finish_batch(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        """Complete several leases in one transaction.

        Each item is checked exactly as `complete` checks it. An item whose lease is stale or
        whose record was purged, changed or hidden since the claim is refused on its own, with
        its error code, and writes nothing; the others are stored."""
        results: list[dict[str, Any]] = []
        for item in args["items"]:
            try:
                results.append(self.finish(who, {"project": args["project"], **item}, True, at))
            except LocalError as exc:
                results.append({"status": "refused", "error": exc.code})
        return {"results": results}

    def claim_one(
        self, who: dict[str, Any], part: Any, args: dict[str, Any], at: int
    ) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT j.* FROM embedding_jobs j JOIN observations o ON j.tenant=o.tenant AND j.observation=o.id WHERE j.tenant=? AND j.project=? AND j.fingerprint=? AND j.generation=? AND o.deleted=0 AND (o.readers='null' OR EXISTS(SELECT 1 FROM json_each(o.readers) WHERE value=?)) AND j.attempts<5 AND ((j.state='pending' AND j.available<=?) OR (j.state='leased' AND j.lease_until<=?)) ORDER BY j.position LIMIT 1",
            (
                who["tenant"],
                args["project"],
                args["fingerprint"],
                part["generation"],
                who["principal"],
                at,
                at,
            ),
        ).fetchone()
        if row is None:
            handed = self.claim_passage(who, part, args, at) or self.claim_revision(
                who, part, args, at
            )
            if handed is not None:
                return handed
            return {"job": None, **self.status(self.partition(who, args))}
        if (
            self.db.execute(
                "SELECT count(*) FROM processing_usage WHERE tenant=?", (who["tenant"],)
            ).fetchone()[0]
            >= USAGE_LIMIT
        ):
            # Only rows past their retention are released; recent attempts are never
            # silently discarded to make room.
            self.db.execute(
                "DELETE FROM processing_usage WHERE tenant=? AND expires<=?", (who["tenant"], at)
            )
            if (
                self.db.execute(
                    "SELECT count(*) FROM processing_usage WHERE tenant=?", (who["tenant"],)
                ).fetchone()[0]
                >= USAGE_LIMIT
            ):
                raise LocalError("usage_retention_limit")
        text, start_byte, end_byte, _ = self.source(who, row, at)
        key = (
            who["tenant"],
            args["project"],
            args["fingerprint"],
            part["generation"],
            row["position"],
        )
        epoch = self.db.execute(
            "SELECT purge_epoch FROM tenants WHERE tenant=?", (who["tenant"],)
        ).fetchone()[0]
        self.db.execute(
            "UPDATE embedding_jobs SET state='leased',fence=fence+1,attempts=attempts+1,lease_until=?,principal=?,purge_epoch=? WHERE tenant=? AND project=? AND fingerprint=? AND generation=? AND position=?",
            (at + 30000, who["principal"], epoch, *key),
        )
        attempt = uid("usage")
        self.db.execute(
            "INSERT INTO processing_usage(tenant,id,project,principal,incarnation,provider,fingerprint,stage,status,input_tokens,cached_input_tokens,output_tokens,cost_microusd,duration_ms,request_hash,output_hash,dependencies,created,expires) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                who["tenant"],
                attempt,
                args["project"],
                who["principal"],
                self.s.incarnation,
                "local-onnx",
                args["fingerprint"],
                "embedding_backfill",
                "started",
                None,
                0,
                0,
                0,
                0,
                canonical_hash(text),
                None,
                json.dumps([{"kind": "observation", "id": row["observation"]}]),
                at,
                at + RETRY_MS,
            ),
        )
        lease = self.s._token(
            "embedding_lease",
            tenant=who["tenant"],
            principal=who["principal"],
            project=args["project"],
            fingerprint=args["fingerprint"],
            generation=part["generation"],
            position=row["position"],
            fence=row["fence"] + 1,
            attempt=attempt,
            input_hash=canonical_hash(text),
        )
        return {
            "job": row["position"],
            "lease": lease,
            "lease_until_ms": at + 30000,
            "input": text,
            "input_hash": canonical_hash(text),
            "fingerprint": args["fingerprint"],
            "dimensions": part["dimensions"],
            "observation": row["observation"],
            "blob_hash": row["blob_hash"],
            "start_byte": start_byte,
            "end_byte": end_byte,
            "egress": "local_cpu_only",
        }

    def finish(
        self, who: dict[str, Any], args: dict[str, Any], complete: bool, at: int
    ) -> dict[str, Any]:
        token = self.s._decode(args["lease"], "embedding_lease")
        if (
            any(token[k] != who[k] for k in ("tenant", "principal"))
            or token["project"] != args["project"]
        ):
            raise LocalError("denied")
        part = self.partition(
            who, {"project": args["project"], "fingerprint": token["fingerprint"]}
        )
        if token.get("subject_kind") == "claim":
            return self.finish_claim(who, args, token, part, complete, at)
        if token.get("subject_kind") == "passage":
            return self.finish_passage(who, args, token, part, complete, at)
        key = (
            who["tenant"],
            args["project"],
            token["fingerprint"],
            token["generation"],
            token["position"],
        )
        row = self.db.execute(
            "SELECT * FROM embedding_jobs WHERE tenant=? AND project=? AND fingerprint=? AND generation=? AND position=?",
            key,
        ).fetchone()
        if (
            row is None
            or row["state"] != "leased"
            or row["fence"] != token["fence"]
            or row["lease_until"] <= at
            or part["generation"] != token["generation"]
            or part["state"] != "experimental"
        ):
            raise LocalError("stale_lease")
        text, start_byte, end_byte, more = self.source(who, row, at)
        epoch = self.db.execute(
            "SELECT purge_epoch FROM tenants WHERE tenant=?", (who["tenant"],)
        ).fetchone()[0]
        if epoch != row["purge_epoch"] or canonical_hash(text) != token["input_hash"]:
            raise LocalError("stale_source")
        output_hash = None
        if complete:
            batch = EmbeddingBatch(
                args["fingerprint"],
                part["dimensions"],
                [args["vector"]],
                [args["input_hash"]],
                args["input_tokens"],
                [args["truncated"]],
            )
            batch.validate(token["fingerprint"], part["dimensions"], [text])
            output_hash = canonical_hash(args["vector"])
            self.write_vector(
                who,
                token,
                args,
                "observation",
                row["observation"],
                0,
                row["source_revision"],
                row["blob_hash"],
                start_byte,
                end_byte,
                more,
                row["position"],
            )
            self.db.execute(
                "DELETE FROM embedding_items WHERE tenant=? AND project=? AND fingerprint=? AND generation=? AND observation=?",
                (*key[:4], row["observation"]),
            )
        state = "complete" if complete else "dead" if row["attempts"] >= 5 else "pending"
        self.db.execute(
            "UPDATE embedding_jobs SET state=?,fence=fence+1,available=? WHERE tenant=? AND project=? AND fingerprint=? AND generation=? AND position=?",
            (state, at + min(60000, 2 ** row["attempts"] * 1000), *key),
        )
        self.db.execute(
            "UPDATE processing_usage SET status=?,input_tokens=?,duration_ms=?,output_hash=? WHERE tenant=? AND id=? AND principal=?",
            (
                "succeeded" if complete else "failed",
                args["input_tokens"],
                args["duration_ms"],
                output_hash,
                who["tenant"],
                token["attempt"],
                who["principal"],
            ),
        )
        self.advance(part)
        return {
            "status": state,
            "fingerprint": token["fingerprint"],
            "observation": row["observation"],
            "usage_attempt": token["attempt"],
        }
