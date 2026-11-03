"""Proposal drafts from repeated evidence: a review queue that is never empty.

Every other memory system lets a model extract facts on write. MemoryKing keeps the
review layer, so this engine only *drafts*: it looks for a name that several
independent observations share (an anchor such as a path, a ticket or a host, or
failing that a rare term), takes the most recent sentence that carries it, and files
an ordinary proposal with one evidence span per supporting observation. The draft is
a `knowledge` claim in state `proposed` with authority left to the reviewer; nothing
it writes can become a constraint or reach a briefing as accepted knowledge until a
person or an authorized reviewer accepts it. Deterministic, bounded, no model.
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Any

from memoryking.contracts.canonical import canonical_hash
from memoryking.domain.briefing import terms

if TYPE_CHECKING:
    from memoryking.adapters.knowledge_store import Knowledge

WINDOW = 512  # newest observations considered
MIN_SUPPORT = 2  # distinct observations before a draft is filed
MAX_SUPPORT = 16  # the contract's evidence cap per revision
MAX_HOLDERS = 32  # an anchor held by more records than this is not a topic but a word
TERM_HOLDERS = 8  # a term shared by more than this many records is too common to anchor
SENTENCE_LIMIT = 512
METHOD = "repeated_evidence_v1"
# A sentence ends at ., ! or ? followed by whitespace or the end, or at a newline, so
# the dot inside `payments/retry.py` or `quartz.internal` never splits one.
_SENTENCE = re.compile(r"[^\n]+?(?:[.!?](?=\s|$)|(?=\n)|$)")


def _sentence_with(text: str, needle: str) -> tuple[int, int, str] | None:
    """The first sentence containing `needle`: byte offsets and the bounded text."""
    wanted = needle.casefold()
    for match in _SENTENCE.finditer(text):
        piece = match.group(0)
        sentence = piece.strip()
        if not sentence:
            continue
        if wanted not in sentence.casefold():
            continue
        start = match.start() + (len(piece) - len(piece.lstrip()))
        sentence = sentence[:SENTENCE_LIMIT]
        byte_start = len(text[:start].encode())
        return byte_start, byte_start + len(sentence.encode()), sentence
    return None


class Proposals:
    def __init__(self, knowledge: Knowledge):
        self.k, self.s, self.db = knowledge, knowledge.s, knowledge.db

    def _window(self, who: dict[str, Any], project: str, at: int) -> list[dict[str, Any]]:
        from memoryking.adapters.retrieval import Retrieval

        retrieval = Retrieval(self.s)
        rows = self.db.execute(
            "SELECT * FROM observations WHERE tenant=? AND project=? AND deleted=0 ORDER BY position DESC,id DESC LIMIT ?",
            (who["tenant"], project, WINDOW),
        ).fetchall()
        items = []
        for row in rows:
            if not self.s._visible(who, row, at):
                continue
            raw = retrieval._blob(row)
            items.append(
                {
                    "id": row["id"],
                    "blob": row["blob"],
                    "position": row["position"],
                    "source_event": row["source_event"],
                    "text": raw.decode("utf-8", "replace"),
                }
            )
        return items

    def _groups(self, who: dict[str, Any], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Candidate topics: shared anchors first, then shared rare terms."""
        by_id = {item["id"]: item for item in items}
        groups: list[dict[str, Any]] = []
        seen_members: set[frozenset[str]] = set()
        if items:
            ids = list(by_id)
            holders: dict[str, list[str]] = {}
            kinds: dict[str, str] = {}
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                for row in self.db.execute(
                    "SELECT id,anchor,kind FROM anchors WHERE tenant=? AND id IN ("
                    + ",".join("?" for _ in chunk)
                    + ") ORDER BY anchor,id",
                    (who["tenant"], *chunk),
                ).fetchall():
                    holders.setdefault(row["anchor"], []).append(row["id"])
                    kinds[row["anchor"]] = row["kind"]
            for anchor, members in sorted(holders.items()):
                unique = sorted(set(members))
                if not MIN_SUPPORT <= len(unique) <= MAX_HOLDERS:
                    continue
                key = frozenset(unique)
                if key in seen_members:
                    continue
                seen_members.add(key)
                groups.append({"topic": anchor, "basis": kinds[anchor], "members": unique})
        if items:
            frequency: dict[str, list[str]] = {}
            for item in items:
                for term in set(terms(item["text"])):
                    if len(term) >= 5:
                        frequency.setdefault(term, []).append(item["id"])
            for term, members in sorted(frequency.items()):
                unique = sorted(set(members))
                if not MIN_SUPPORT <= len(unique) <= TERM_HOLDERS:
                    continue
                key = frozenset(unique)
                if key in seen_members:
                    continue
                seen_members.add(key)
                groups.append({"topic": term, "basis": "term", "members": unique})
        return groups

    def draft(
        self, credential: str, who: dict[str, Any], args: dict[str, Any], at: int
    ) -> dict[str, Any]:
        project = args["project"]
        limit = args.get("limit", 16)
        items = self._window(who, project, at)
        by_id = {item["id"]: item for item in items}
        drafted: list[dict[str, Any]] = []
        skipped: dict[str, int] = {"already_proposed": 0, "single_source": 0, "no_sentence": 0}
        for group in self._groups(who, items):
            if len(drafted) >= limit:
                skipped["limit"] = skipped.get("limit", 0) + 1
                continue
            members = [by_id[i] for i in group["members"]]
            # Repetition means independent occurrences, not one event captured twice.
            if len({m["source_event"] for m in members}) < MIN_SUPPORT:
                skipped["single_source"] += 1
                continue
            spans = []
            sentence = None
            for member in sorted(members, key=lambda m: -m["position"])[:MAX_SUPPORT]:
                found = _sentence_with(member["text"], group["topic"])
                if found is None:
                    continue
                start, end, text = found
                if sentence is None:
                    sentence = text
                spans.append(
                    {
                        "observation": member["id"],
                        "start_byte": start,
                        "end_byte": end,
                        "content_hash": member["blob"],
                    }
                )
            if sentence is None or len(spans) < MIN_SUPPORT:
                skipped["no_sentence"] += 1
                continue
            logical_key = "draft-" + hashlib.sha256(group["topic"].encode()).hexdigest()[:40]
            family = self.db.execute(
                "SELECT id,version FROM families WHERE tenant=? AND project=? AND logical_key=?",
                (who["tenant"], project, logical_key),
            ).fetchone()
            version = 0
            if family is not None:
                version = family["version"]
                latest = self.db.execute(
                    "SELECT id,purged FROM revisions WHERE tenant=? AND family=? ORDER BY created DESC LIMIT 1",
                    (who["tenant"], family["id"]),
                ).fetchone()
                if latest is not None and not latest["purged"]:
                    existing = {
                        row["observation"]
                        for row in self.db.execute(
                            "SELECT observation FROM evidence_spans WHERE tenant=? AND revision=?",
                            (who["tenant"], latest["id"]),
                        ).fetchall()
                    }
                    if {s["observation"] for s in spans} <= existing:
                        skipped["already_proposed"] += 1
                        continue
            body = {
                "text": sentence,
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
                "provenance": {
                    "method": METHOD,
                    "topic": group["topic"],
                    "basis": group["basis"],
                    "support": len(spans),
                    "drafted_by": who["principal"],
                },
            }
            result = self.k.propose(
                credential,
                who,
                {
                    "project": project,
                    "logical_key": logical_key,
                    "expected_version": version,
                    "body": body,
                },
                at,
            )
            drafted.append(
                {
                    "revision": result["revision"],
                    "family": result["family"],
                    "topic": group["topic"],
                    "basis": group["basis"],
                    "support": len(spans),
                    "text": sentence,
                }
            )
        return {
            "drafted": drafted,
            "skipped": skipped,
            "window": len(items),
            "method": METHOD,
            "coverage": f"newest_{WINDOW}_visible_observations; anchors_then_rare_terms; drafts_are_proposals_awaiting_review",
            "digest": canonical_hash([d["revision"] for d in drafted]),
        }
