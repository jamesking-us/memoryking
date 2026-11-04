"""Durable, opt-in embedding projection jobs under the existing release gate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from memoryking.adapters.local_embeddings import MODEL, model_manifest
from memoryking.adapters.local_store import MAX_COUNTER, RETRY_MS, USAGE_LIMIT, uid
from memoryking.adapters.retrieval import Retrieval
from memoryking.application.store_core import StoreCore
from memoryking.contracts.canonical import canonical_bytes, canonical_hash
from memoryking.domain.embeddings import EmbeddingBatch, fingerprint
from memoryking.domain.local import LocalError, Released

VALIDATOR = Draft202012Validator(
    json.loads(Path(__file__).with_name("embedding.schema.json").read_text())
)


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
            elif action in {"complete", "fail"}:
                result = self.finish(who, args, action == "complete", at)
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
                "INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    who["tenant"],
                    receipt,
                    who["principal"],
                    self.s.incarnation,
                    at,
                    decision.deadline_ms,
                    *epochs,
                    hashlib.sha256(raw).hexdigest(),
                    json.dumps([result["observation"]] if "observation" in result else []),
                ),
            )
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
        if args["model"] != MODEL:
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
        configuration = model_manifest()
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
        for table in ("embedding_jobs", "embedding_items"):
            self.db.execute(
                f"DELETE FROM {table} WHERE tenant=? AND project=? AND fingerprint=?", key
            )
        self.db.execute(
            "INSERT INTO embedding_partitions VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(tenant,project,fingerprint) DO UPDATE SET incarnation=excluded.incarnation,generation=excluded.generation,configuration=excluded.configuration,state=excluded.state,scheduled=0,frontier=0",
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
            "INSERT INTO retries VALUES (?,?,?,?,?,?,?,?,?,?)",
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
                "INSERT INTO embedding_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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

    def source(self, who: dict[str, Any], row: Any, at: int) -> str:
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
        return self.r._blob(observation)[:16384].decode("utf-8", errors="ignore")

    def claim(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        part = self.partition(who, args)
        if part["state"] != "experimental":
            raise LocalError("embedding_disabled")
        self.schedule(part, at)
        self.db.execute(
            "UPDATE embedding_jobs SET state='dead',fence=fence+1 WHERE tenant=? AND state='leased' AND lease_until<=? AND attempts>=5",
            (who["tenant"], at),
        )
        if (
            self.db.execute(
                "SELECT count(*) FROM embedding_jobs WHERE tenant=? AND state='leased' AND lease_until>?",
                (who["tenant"], at),
            ).fetchone()[0]
            >= 4
        ):
            raise LocalError("concurrency_limit")
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
        text = self.source(who, row, at)
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
            "INSERT INTO processing_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
            "start_byte": 0,
            "end_byte": len(text.encode()),
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
        text = self.source(who, row, at)
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
            self.db.execute(
                "INSERT INTO embedding_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(tenant,project,fingerprint,generation,observation) DO UPDATE SET incarnation=excluded.incarnation,source_revision=excluded.source_revision,blob_hash=excluded.blob_hash,input_hash=excluded.input_hash,vector_json=excluded.vector_json,start_byte=excluded.start_byte,end_byte=excluded.end_byte,input_tokens=excluded.input_tokens,truncated=excluded.truncated,position=excluded.position",
                (
                    *key[:4],
                    row["observation"],
                    self.s.incarnation,
                    row["source_revision"],
                    row["blob_hash"],
                    args["input_hash"],
                    json.dumps(args["vector"]),
                    0,
                    len(text.encode()),
                    args["input_tokens"],
                    int(args["truncated"]),
                    row["position"],
                ),
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
