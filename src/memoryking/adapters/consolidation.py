"""Consolidation that drafts: a model proposes claims from recent evidence, the store verifies.

Two store actions bracket one model call that the host makes through
`AuthorizedConversation` (see `memoryking.consolidation`):

* `consolidate_prepare` selects the visible observations recorded after the project's
  consolidation frontier, with the few accepted claims that share words with them, and
  retains that input as an artifact with its own receipt. The artifact is what the model
  is shown, as user-role data.
* `consolidate_admit` takes the model's reply and admits it only when it is byte for byte
  the delivered reply in this principal's egress audit for a permit that cited that
  receipt. Each claim must quote, verbatim after whitespace folding, observations that
  were in the input and are still readable; anything else is dropped and counted, never
  repaired. An admitted claim is an ordinary proposal: state `proposed`, no authority,
  evidence spans that are the byte ranges of the quotes, provenance naming the method,
  provider, model, permit and prompt version.

What a model can cause is therefore bounded here, not in the prompt: proposals with
verified quotes, and a proposed replacement of an accepted claim registered as a conflict.
It cannot accept, reject, purge, grant, add constraints or reach any tool.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, Any

from memoryking.adapters.egress_policy import rank
from memoryking.adapters.local_store import uid
from memoryking.adapters.retrieval import Retrieval
from memoryking.application.storage import insert_ignore
from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import LocalError, Released

if TYPE_CHECKING:
    from memoryking.adapters.knowledge_store import Knowledge

PROMPT_VERSION = "consolidation-v1"
METHOD = "model_consolidation_v1"
KINDS = ("fact", "decision", "preference", "instruction", "failed_approach")
RELATIONS = ("contradicts", "updates")
OBSERVATION_BYTES = 2048  # the model sees at most this much of one observation
BATCH_BYTES = 36000  # observation text in one input
CONTEXT_BYTES = 8000  # accepted-claim text in one input
CONTEXT_CLAIMS = 8
SCAN_LIMIT = 1024  # observations examined past the frontier in one pass
REPLY_LIMIT = 32768  # a larger reply is rejected whole
CLAIM_LIMIT = 32
TEXT_LIMIT = 600  # characters in a drafted claim
QUOTE_MIN = 8  # characters in a quote, after whitespace folding
QUOTE_MAX = 1024
EVIDENCE_LIMIT = 8  # quotes per claim
KNOWN_LIMIT = 2048  # newest claims compared for duplicates
STALE_MS = 600_000  # a prepared input nobody admitted within ten minutes is abandoned


def fold(text: str) -> tuple[str, list[int]]:
    """Collapse each run of whitespace to one space and drop leading and trailing runs.

    Returns the folded text and, for every folded character, its index in `text`. The same
    folding is applied to a quote and to the observation before they are compared, so the
    comparison is otherwise exact: no case folding, no Unicode normalisation."""
    out: list[str] = []
    where: list[int] = []
    gap: int | None = None
    for index, char in enumerate(text):
        if char.isspace():
            if out and gap is None:
                gap = index
            continue
        if gap is not None:
            out.append(" ")
            where.append(gap)
            gap = None
        out.append(char)
        where.append(index)
    return "".join(out), where


def locate(quote: str, text: str) -> tuple[int, int] | None:
    """The first place `quote` occurs in `text` (whitespace folded on both sides) as a
    byte range of `text`, or None."""
    wanted, _ = fold(quote)
    if not QUOTE_MIN <= len(wanted) <= QUOTE_MAX:
        return None
    folded, where = fold(text)
    at = folded.find(wanted)
    if at < 0:
        return None
    start = where[at]
    end = where[at + len(wanted) - 1] + 1
    return len(text[:start].encode()), len(text[:end].encode())


def sameness(text: str) -> str:
    """The key two claims are the same under: folded, case-folded, no closing punctuation."""
    return fold(text)[0].casefold().rstrip(" .!?;:")


def parse_reply(text: str) -> list[Any]:
    """The `claims` list of a strict JSON reply. One surrounding code fence is tolerated
    because models add it unasked; nothing inside is repaired."""
    if len(text.encode()) > REPLY_LIMIT:
        raise ValueError("reply_too_large")
    body = text.strip()
    fenced = re.fullmatch(r"```(?:json)?[ \t]*\n(.*)\n```", body, re.DOTALL)
    if fenced:
        body = fenced.group(1)
    try:
        value = json.loads(body)
    except ValueError as exc:
        raise ValueError("reply_not_json") from exc
    if not isinstance(value, dict) or set(value) != {"claims"} or not isinstance(
        value["claims"], list
    ):
        raise ValueError("reply_shape_invalid")
    return value["claims"]


def tokens(text: str) -> set[str]:
    return set(re.findall(r"[^\W_]{4,}", text.casefold()))


class Consolidation:
    def __init__(self, knowledge: Knowledge):
        self.k, self.s, self.db = knowledge, knowledge.s, knowledge.db
        self.r = Retrieval(knowledge.s)

    # --- Prepare ---------------------------------------------------------------------

    def frontier(self, tenant: str, project: str) -> int:
        row = self.db.execute(
            "SELECT max(to_position) FROM consolidation_runs WHERE tenant=? AND project=? AND state='admitted'",
            (tenant, project),
        ).fetchone()
        return int(row[0] or 0)

    def prepare(self, credential: str, command: dict[str, Any]) -> Released:
        args = command["args"]
        project, receipt = args["project"], uid("rcpt")
        with self.s.transaction() as decision:
            at = decision.evaluated_ms
            who = self.s._auth(credential, "capture", project, at)
            self.s._auth(credential, "read", project, at)
            self.r._expire(at)
            packet, deps, run = self._input(who, args, decision, receipt, at)
            raw = canonical_bytes(packet)
            if run is not None:
                diag = {
                    "status": "complete",
                    "kind": "consolidation_input",
                    "prompt_version": PROMPT_VERSION,
                    "usage": {
                        "output_tokens": len(raw),
                        "max_output_tokens": None,
                        "serialized_bytes": len(raw),
                        "counting": "estimate",
                        "tokenizer": "utf8-byte-estimate-v1",
                        "provider_usage": "not_used",
                    },
                    "trace": [],
                }
                self.r._retain(who, {"project": project}, receipt, raw, diag, deps, at)
                self.db.execute(
                    "INSERT INTO consolidation_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        who["tenant"],
                        receipt,
                        project,
                        who["principal"],
                        "prepared",
                        run["from"],
                        run["to"],
                        run["observations"],
                        PROMPT_VERSION,
                        None,
                        None,
                        None,
                        0,
                        json.dumps(run["skipped"]),
                        at,
                        None,
                    ),
                )
            else:
                raw = canonical_bytes({**packet, "receipt": receipt})
            epochs = self.db.execute(
                "SELECT policy_epoch,purge_epoch FROM tenants WHERE tenant=?", (who["tenant"],)
            ).fetchone()
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
                    json.dumps([d["id"] for d in deps if d["kind"] == "observation"]),
                ),
            )
            self.s.fault("after_validation")
        self.s._expose()
        return Released(raw, decision, self.s.clock, receipt)

    def _input(
        self, who: dict[str, Any], args: dict[str, Any], decision: Any, receipt: str, at: int
    ) -> tuple[dict[str, Any], list[dict[str, str]], dict[str, Any] | None]:
        tenant, project = who["tenant"], args["project"]
        self.db.execute(
            "UPDATE consolidation_runs SET state='abandoned',closed=? WHERE tenant=? AND project=? AND state='prepared' AND created<?",
            (at, tenant, project, at - STALE_MS),
        )
        frontier = self.frontier(tenant, project)
        none: dict[str, Any] = {"version": 1, "status": "nothing_new", "frontier": frontier}
        if args.get("idle_ms"):
            newest = self.db.execute(
                "SELECT max(e.at_ms) FROM observations o JOIN events e ON e.tenant=o.tenant AND e.position=o.position WHERE o.tenant=? AND o.project=? AND o.position>?",
                (tenant, project, frontier),
            ).fetchone()[0]
            if newest is not None and at - int(newest) < args["idle_ms"]:
                return {**none, "status": "busy"}, [], None
        ceiling = rank(args.get("max_classification", "restricted"))
        since = args.get("since_ms")
        limit = args.get("limit", 24)
        chosen: list[dict[str, Any]] = []
        skipped = {"classification": 0, "unreadable": 0}
        size, last, scanned = 0, frontier, 0
        cursor = frontier
        while len(chosen) < limit and scanned < SCAN_LIMIT:
            rows = self.db.execute(
                "SELECT o.* FROM observations o"
                + (" JOIN events e ON e.tenant=o.tenant AND e.position=o.position" if since else "")
                + " WHERE o.tenant=? AND o.project=? AND o.deleted=0 AND o.position>?"
                + (" AND e.at_ms>=?" if since else "")
                + " ORDER BY o.position LIMIT 128",
                (tenant, project, cursor, *((since,) if since else ())),
            ).fetchall()
            if not rows:
                break
            for row in rows:
                scanned += 1
                cursor = row["position"]
                if len(chosen) >= limit:
                    break
                if not self.s._visible(who, row, at):
                    continue
                if rank(row["classification"]) > ceiling:
                    skipped["classification"] += 1
                    last = row["position"]
                    continue
                try:
                    text = self.r._blob(row).decode("utf-8")
                except (UnicodeDecodeError, LocalError):
                    skipped["unreadable"] += 1
                    last = row["position"]
                    continue
                shown = text.encode()[:OBSERVATION_BYTES].decode("utf-8", "ignore")
                if chosen and size + len(shown.encode()) > BATCH_BYTES:
                    scanned = SCAN_LIMIT
                    break
                size += len(shown.encode())
                last = row["position"]
                chosen.append(
                    {
                        "id": row["id"],
                        "source": row["source"],
                        "text": shown,
                        "truncated": len(shown.encode()) < len(text.encode()),
                        "position": row["position"],
                    }
                )
        if not chosen:
            return none, [], None
        deps = [{"kind": "observation", "id": o["id"]} for o in chosen]
        claims = self._context(who, project, chosen, at)
        deps.extend({"kind": "revision", "id": c["id"]} for c in claims)
        packet = {
            "version": 1,
            "status": "complete",
            "schema": "consolidation-input-v1",
            "receipt": receipt,
            "incarnation": self.s.incarnation,
            "evaluated_ms": at,
            "deadline_ms": decision.deadline_ms,
            "project": project,
            "observations": [{k: v for k, v in o.items() if k != "position"} for o in chosen],
            "accepted_claims": claims,
        }
        run = {
            "from": frontier,
            "to": last,
            "observations": len(chosen),
            "skipped": skipped,
        }
        return packet, deps, run

    def _context(
        self, who: dict[str, Any], project: str, chosen: list[dict[str, Any]], at: int
    ) -> list[dict[str, str]]:
        """Accepted claims that share words with the batch, so the model can name the one a
        new statement contradicts or updates. Readable claims only, bounded."""
        batch = tokens(" ".join(o["text"] for o in chosen))
        scored: list[tuple[int, int, dict[str, str]]] = []
        for row in self.db.execute(
            "SELECT r.*,f.project FROM revisions r JOIN families f ON f.tenant=r.tenant AND f.id=r.family WHERE r.tenant=? AND f.project=? AND r.kind='knowledge' AND r.purged=0 ORDER BY r.created DESC LIMIT 512",
            (who["tenant"], project),
        ).fetchall():
            state, quarantine = self.k._state(who["tenant"], row["id"], 9_007_199_254_740_991)
            if state != "accepted" or quarantine or not self.k._available(who, row, at):
                continue
            text = json.loads(row["body"])["text"]
            shared = len(batch & tokens(text))
            if shared >= 2:
                scored.append((-shared, -row["created"], {"id": row["id"], "text": text[:600]}))
        scored.sort(key=lambda item: item[:2])
        claims, size = [], 0
        for _, _, claim in scored[:CONTEXT_CLAIMS]:
            size += len(claim["text"].encode())
            if size > CONTEXT_BYTES:
                break
            claims.append(claim)
        return claims

    # --- Admit -----------------------------------------------------------------------

    def admit(
        self, credential: str, who: dict[str, Any], args: dict[str, Any], at: int
    ) -> dict[str, Any]:
        tenant, project = who["tenant"], args["project"]
        run = self.db.execute(
            "SELECT * FROM consolidation_runs WHERE tenant=? AND id=? AND project=? AND principal=?",
            (tenant, args["run"], project, who["principal"]),
        ).fetchone()
        if run is None or run["state"] != "prepared":
            raise LocalError("consolidation_run_invalid")
        text = args["text"]
        audit = self.db.execute(
            "SELECT * FROM egress_audit WHERE tenant=? AND id=? AND principal=? AND project=?",
            (tenant, args["permit"], who["principal"], project),
        ).fetchone()
        if (
            audit is None
            or audit["status"] != "delivered"
            or audit["response_hash"] != hashlib.sha256(text.encode()).hexdigest()
        ):
            raise LocalError("consolidation_source_invalid")
        manifest = json.loads(audit["manifest"])
        cited = {
            a["receipt"]
            for proof in [*manifest["segments"], *manifest["handles"]]
            for a in proof["artifacts"]
        }
        try:
            artifact = self.r._artifact(who, project, run["id"], at)
        except LocalError as exc:
            raise LocalError("consolidation_source_invalid") from exc
        if run["id"] not in cited:
            raise LocalError("consolidation_source_invalid")
        delivered = json.loads(artifact["body"])
        sent = {o["id"]: o["text"] for o in delivered["observations"]}
        offered = {c["id"] for c in delivered["accepted_claims"]}
        dropped: dict[str, int] = {}

        def drop(reason: str) -> None:
            dropped[reason] = dropped.get(reason, 0) + 1

        filed: list[dict[str, Any]] = []
        try:
            claims = parse_reply(text)
        except ValueError as exc:
            drop(str(exc))
            claims = []
        known = self._known(tenant, project, at) if claims else ({}, {})
        seen_text: set[str] = set()
        for number, claim in enumerate(claims):
            if number >= CLAIM_LIMIT:
                drop("claim_limit")
                continue
            made = self._claim(who, project, claim, sent, offered, known, seen_text, at, drop)
            if made is None:
                continue
            body, relation, anchor = made
            body["provenance"] = {
                "method": METHOD,
                "prompt_version": PROMPT_VERSION,
                "provider": audit["provider"],
                "model": audit["model"],
                "permit": audit["id"],
                "run": run["id"],
                "claim_kind": claim["kind"],
                "drafted_by": who["principal"],
                **({"relation": relation} if relation else {}),
            }
            try:
                result = self.k.propose(
                    credential,
                    who,
                    {
                        "project": project,
                        "logical_key": anchor["logical_key"],
                        "expected_version": anchor["version"],
                        "body": body,
                    },
                    at,
                )
            except LocalError:
                drop("refused_by_store")
                continue
            seen_text.add(sameness(body["text"]))
            known[1][
                frozenset((e["observation"], e["start_byte"], e["end_byte"]) for e in body["evidence"])
            ] = "proposed"
            if relation:
                self.db.execute(
                    insert_ignore("conflicts", 4),
                    (tenant, relation["claim"], result["revision"], result["position"]),
                )
            filed.append(
                {
                    "revision": result["revision"],
                    "family": result["family"],
                    "kind": claim["kind"],
                    **({"relation": relation} if relation else {}),
                }
            )
        accepted_reply = not any(
            reason in dropped for reason in ("reply_too_large", "reply_not_json", "reply_shape_invalid")
        )
        self.db.execute(
            "UPDATE consolidation_runs SET state=?,permit=?,provider=?,model=?,filed=?,dropped=?,closed=? WHERE tenant=? AND id=?",
            (
                "admitted" if accepted_reply else "rejected",
                audit["id"],
                audit["provider"],
                audit["model"],
                len(filed),
                json.dumps({"dropped": dropped, "skipped": json.loads(run["dropped"])}),
                at,
                tenant,
                run["id"],
            ),
        )
        return {
            "run": run["id"],
            "status": "admitted" if accepted_reply else "rejected",
            "filed": filed,
            "dropped": dict(sorted(dropped.items())),
            "claims_seen": len(claims),
            "observations": run["observations"],
            "frontier": self.frontier(tenant, project) if accepted_reply else run["from_position"],
            "prompt_version": PROMPT_VERSION,
        }

    def _known(
        self, tenant: str, project: str, at: int
    ) -> tuple[dict[str, str], dict[frozenset[tuple[str, int, int]], str]]:
        """Claims already in the queue or accepted, by text and by evidence, plus rejected
        text: a reviewer's rejection of a statement is not asked again."""
        by_text: dict[str, str] = {}
        by_evidence: dict[frozenset[tuple[str, int, int]], str] = {}
        for row in self.db.execute(
            "SELECT r.id,r.body FROM revisions r JOIN families f ON f.tenant=r.tenant AND f.id=r.family WHERE r.tenant=? AND f.project=? AND r.kind='knowledge' AND r.purged=0 ORDER BY r.created DESC LIMIT ?",
            (tenant, project, KNOWN_LIMIT),
        ).fetchall():
            state, _ = self.k._state(tenant, row["id"], 9_007_199_254_740_991)
            if state not in {"proposed", "accepted", "rejected"} or row["body"] is None:
                continue
            body = json.loads(row["body"])
            by_text.setdefault(sameness(body["text"]), state)
            if state != "rejected":
                key = frozenset(
                    (s["observation"], s["start_byte"], s["end_byte"]) for s in body["evidence"]
                )
                by_evidence.setdefault(key, state)
        return by_text, by_evidence

    def _claim(
        self,
        who: dict[str, Any],
        project: str,
        claim: Any,
        sent: dict[str, str],
        offered: set[str],
        known: tuple[dict[str, str], dict[frozenset[tuple[str, int, int]], str]],
        seen_text: set[str],
        at: int,
        drop: Any,
    ) -> tuple[dict[str, Any], dict[str, str] | None, dict[str, Any]] | None:
        tenant = who["tenant"]
        if (
            not isinstance(claim, dict)
            or not {"text", "kind", "evidence"} <= set(claim)
            or not set(claim) <= {"text", "kind", "evidence", "relation"}
        ):
            drop("invalid_shape")
            return None
        text, kind, evidence = claim["text"], claim["kind"], claim["evidence"]
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= TEXT_LIMIT:
            drop("invalid_text")
            return None
        if kind not in KINDS:
            drop("invalid_kind")
            return None
        if not isinstance(evidence, list) or not evidence:
            drop("no_evidence")
            return None
        if len(evidence) > EVIDENCE_LIMIT:
            drop("evidence_limit")
            return None
        spans: list[dict[str, Any]] = []
        for item in evidence:
            if (
                not isinstance(item, dict)
                or set(item) != {"observation", "quote"}
                or not isinstance(item["observation"], str)
                or not isinstance(item["quote"], str)
            ):
                drop("invalid_shape")
                return None
            if item["observation"] not in sent:
                drop("unknown_observation")
                return None
            row = self.db.execute(
                "SELECT * FROM observations WHERE tenant=? AND id=?",
                (tenant, item["observation"]),
            ).fetchone()
            if row is None or not self.s._visible(who, row, at):
                drop("unreadable_observation")
                return None
            try:
                current = self.r._blob(row).decode("utf-8")
            except (UnicodeDecodeError, LocalError):
                drop("unreadable_observation")
                return None
            if not current.encode().startswith(sent[item["observation"]].encode()):
                drop("observation_changed")
                return None
            # The quote must be in what the model was shown, and the byte range is read
            # from the stored text, which begins with it.
            if locate(item["quote"], sent[item["observation"]]) is None:
                drop("quote_not_verbatim")
                return None
            found = locate(item["quote"], current)
            if found is None:
                drop("quote_not_verbatim")
                return None
            span = {
                "observation": item["observation"],
                "start_byte": found[0],
                "end_byte": found[1],
                "content_hash": row["blob"],
            }
            if span not in spans:
                spans.append(span)
        relation: dict[str, str] | None = None
        anchor: dict[str, Any] = {}
        if "relation" in claim:
            raw = claim["relation"]
            if (
                not isinstance(raw, dict)
                or set(raw) != {"type", "claim"}
                or raw["type"] not in RELATIONS
                or not isinstance(raw["claim"], str)
                or raw["claim"] not in offered
            ):
                drop("invalid_relation")
                return None
            target = self.k._revision(tenant, raw["claim"])
            state, quarantine = self.k._state(tenant, target["id"], 9_007_199_254_740_991)
            if state != "accepted" or quarantine or not self.k._available(who, target, at):
                drop("invalid_relation")
                return None
            family = self.db.execute(
                "SELECT logical_key,version FROM families WHERE tenant=? AND id=?",
                (tenant, target["family"]),
            ).fetchone()
            relation = {"type": raw["type"], "claim": target["id"]}
            anchor = {"logical_key": family["logical_key"], "version": family["version"]}
        key = sameness(text)
        by_text, by_evidence = known
        if key in seen_text:
            drop("duplicate")
            return None
        if by_text.get(key) == "rejected":
            drop("previously_rejected")
            return None
        if key in by_text:
            drop("duplicate")
            return None
        if by_evidence.get(frozenset((s["observation"], s["start_byte"], s["end_byte"]) for s in spans)):
            drop("duplicate")
            return None
        if not anchor:
            logical = "cons-" + hashlib.sha256(key.encode()).hexdigest()[:40]
            family = self.db.execute(
                "SELECT version FROM families WHERE tenant=? AND project=? AND logical_key=?",
                (tenant, project, logical),
            ).fetchone()
            anchor = {"logical_key": logical, "version": family["version"] if family else 0}
        body = {
            "text": text.strip(),
            "kind": "knowledge",
            "selector": {"selector_version": "1", "any_of": [{"all_of": [{"op": "any"}]}]},
            "valid_start": None,
            "valid_end": None,
            "task": None,
            "run": None,
            "readers": None,
            "evidence": spans,
            "authority": None,
            "rule_key": None,
            "effect": None,
            "rule_value": None,
        }
        return body, relation, anchor
