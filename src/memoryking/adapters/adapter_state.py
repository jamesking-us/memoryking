"""Current-authorized resume deltas with independent full model-artifact retention."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from memoryking.adapters.inference import EGRESS_REVISION
from memoryking.adapters.local_store import MAX_COUNTER, uid
from memoryking.adapters.token_count import counter
from memoryking.contracts.canonical import canonical_bytes, canonical_hash
from memoryking.domain.briefing import counted
from memoryking.domain.delta import MAX_BYTES, SCHEMA, SERIALIZATION, apply, changes
from memoryking.domain.local import Decision, LocalError

if TYPE_CHECKING:
    from memoryking.adapters.working import Working


def resume(
    working: Working, who: dict[str, Any], args: dict[str, Any], decision: Decision, receipt: str
) -> tuple[bytes, list[dict[str, str]]]:
    s, db, r, at = working.s, working.db, working.r, decision.evaluated_ms
    epochs = db.execute(
        "SELECT policy_epoch,purge_epoch FROM tenants WHERE tenant=?", (who["tenant"],)
    ).fetchone()
    binding = {
        "store": s.store_id,
        "tenant": who["tenant"],
        "principal": who["principal"],
        "incarnation": s.incarnation,
        "project": args["project"],
        "authorization_epoch": epochs[0],
        "purge_epoch": epochs[1],
        "delegation_chain": who.get("delegation_chain", []),
        "egress_policy_revision": EGRESS_REVISION,
        "model_session": args["adapter_context"]["model_session"],
        "context_epoch": args["adapter_context"]["context_epoch"],
        "view_hash": canonical_hash(
            {
                key: args[key]
                for key in ("project", "task", "run", "checkpoint", "bindings", "tokenizer")
            }
        ),
    }
    context_receipt = uid("rcpt")
    packet_raw, deps = working._resume(who, args, decision, context_receipt)
    packet = json.loads(packet_raw)
    db.execute(
        "INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            who["tenant"],
            context_receipt,
            who["principal"],
            s.incarnation,
            at,
            decision.deadline_ms,
            *epochs,
            hashlib.sha256(packet_raw).hexdigest(),
            json.dumps([d["id"] for d in deps if d["kind"] == "observation"]),
        ),
    )
    db.execute("DELETE FROM adapter_bases WHERE tenant=? AND expires<=?", (who["tenant"], at))
    base = None
    base_deps: list[dict[str, str]] = []
    reason = "new_base"
    offered = args["base"]
    if offered is not None:
        reason = "base_unavailable"
        row = db.execute(
            "SELECT id,binding,dependencies,body_hash FROM adapter_bases WHERE tenant=? AND principal=? AND project=? AND incarnation=? AND id=? AND expires>?",
            (who["tenant"], who["principal"], args["project"], s.incarnation, offered["id"], at),
        ).fetchone()
        if row is not None:
            reason = "binding_changed"
            if json.loads(row["binding"]) == binding:
                reason = "base_incompatible"
                if row["body_hash"] == offered["hash"] and offered["schema"] == SCHEMA:
                    try:
                        for dep in json.loads(row["dependencies"]):
                            r._dependency(who, dep, at)
                        body = bytes(
                            db.execute(
                                "SELECT body FROM adapter_bases WHERE tenant=? AND id=?",
                                (who["tenant"], row["id"]),
                            ).fetchone()[0]
                        )
                        candidate = json.loads(body)
                        if (
                            hashlib.sha256(body).hexdigest() == offered["hash"]
                            and candidate["sequence"] == offered["sequence"]
                            and candidate["sequence"] < MAX_COUNTER
                        ):
                            previous = candidate["packet"].get("checkpoint")
                            current = packet.get("checkpoint")
                            if (
                                previous
                                and current
                                and 0 <= current["revision"] - previous["revision"] <= 1
                            ):
                                base = candidate
                                base_deps = json.loads(row["dependencies"])
                            else:
                                reason = "revision_gap"
                    except (LocalError, ValueError, KeyError, TypeError):
                        reason = "base_unavailable"
    target = {"sequence": base["sequence"] + 1 if base else 1, "packet": packet}
    count, usage = counter(args["tokenizer"])
    envelope = {
        "version": 1,
        "status": packet["status"],
        "schema": SCHEMA,
        "serialization": SERIALIZATION,
        "mode": "full",
        "binding": binding,
        "sequence": target["sequence"],
        "base_hash": None,
        "target_hash": canonical_hash(target),
        "base_id": receipt,
        "receipt": receipt,
        "evaluated_ms": at,
        "deadline_ms": decision.deadline_ms,
        "target": target,
        "fallback_reason": reason,
        "usage": {
            **usage,
            "max_output_tokens": args["max_output_tokens"],
            "budget_compliant": True,
            "materialized_tokens": count(packet_raw.decode()),
            "materialized_bytes": len(packet_raw),
            "model_context": "full_essential_state",
            "provider_usage": "unknown",
        },
    }
    full_raw, _ = counted(envelope, count)
    if base is not None:
        try:
            operations = changes(base, target)
            if canonical_hash(apply(base, operations)) != canonical_hash(target):
                raise LocalError("delta_target_mismatch")
            delta = {key: value for key, value in envelope.items() if key != "target"}
            before = {(dep["kind"], dep["id"]) for dep in base_deps}
            after = {(dep["kind"], dep["id"]) for dep in deps}
            changed_references = [
                {"kind": kind, "id": identifier, "change": change}
                for change, items in (("removed", before - after), ("added", after - before))
                for kind, identifier in sorted(items)
            ]
            if len(changed_references) > 256:
                raise LocalError("delta_limit")
            delta.update(
                mode="delta",
                base_hash=canonical_hash(base),
                changes=operations,
                fallback_reason=None,
                changed_references=changed_references,
                essential_state_changes=[
                    operation["path"]
                    for operation in operations
                    if operation["path"][:3] == ["packet", "checkpoint", "state"]
                    or operation["path"][:2]
                    in (["packet", "constraints"], ["packet", "operations"])
                ],
            )
            delta_raw, delta_tokens = counted(delta, count)
            if len(delta_raw) < len(full_raw) and delta_tokens <= args["max_output_tokens"]:
                envelope = delta
            else:
                envelope["fallback_reason"] = "no_transport_savings"
        except LocalError:
            envelope["fallback_reason"] = "delta_limit"
    raw, used = counted(envelope, count)
    if used > args["max_output_tokens"] or len(raw) > MAX_BYTES:
        envelope = {
            key: value for key, value in envelope.items() if key not in {"target", "changes"}
        }
        envelope.update(
            status="budget_insufficient",
            mode="unavailable",
            base_id=None,
            minimum_required_tokens=used,
            reason="adapter_envelope_does_not_fit",
        )
        envelope["usage"]["budget_compliant"] = False
        raw, used = counted(envelope, count)
        deps = []
    else:
        # Bounded convenience state: evict the oldest owned base before adding.
        owned = db.execute(
            "SELECT id FROM adapter_bases WHERE tenant=? AND principal=? ORDER BY created,id",
            (who["tenant"], who["principal"]),
        ).fetchall()
        for row in owned[: max(0, len(owned) - 63)]:
            db.execute(
                "DELETE FROM adapter_bases WHERE tenant=? AND id=?", (who["tenant"], row["id"])
            )
        total, size = db.execute(
            "SELECT count(*),coalesce(sum(length(body)),0) FROM adapter_bases WHERE tenant=?",
            (who["tenant"],),
        ).fetchone()
        target_raw = canonical_bytes(target)
        if total < 512 and size + len(target_raw) <= 67108864:
            db.execute(
                "INSERT INTO adapter_bases VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    who["tenant"],
                    receipt,
                    args["project"],
                    who["principal"],
                    s.incarnation,
                    json.dumps(binding),
                    target_raw,
                    canonical_hash(target),
                    json.dumps(deps),
                    at,
                    at + 300000,
                ),
            )
        else:
            envelope["base_id"] = None
            raw, used = counted(envelope, count)
            if used > args["max_output_tokens"] or len(raw) > MAX_BYTES:
                raise LocalError("budget_insufficient")
    r._retain(
        who,
        args,
        receipt,
        raw,
        {
            "kind": "adapter_resume",
            "status": envelope["status"],
            "mode": envelope["mode"],
            "context_receipt": context_receipt,
            "trace": [],
            "usage": {**envelope["usage"], "output_tokens": used, "serialized_bytes": len(raw)},
            "fallback_reason": envelope["fallback_reason"],
        },
        deps,
        at,
    )
    return raw, deps
