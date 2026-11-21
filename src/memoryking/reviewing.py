"""Review from the terminal: list waiting proposals, show one with its evidence, decide.

Every decision goes through the existing `knowledge` operation with the caller's own
credential, so the review capability (and `policy` for a constraint) is checked by the
daemon exactly as for the dashboard. The terminal adds nothing the API lacks: it pages the
reviewer's listing, reads one revision with the sentences it cites, and sends `review`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from memoryking import hooks
from memoryking.adapters.local_ipc import request
from memoryking.domain.local import LocalError

PAGES = 20  # listing pages read at most (256 revisions each) to find the waiting ones
ANY = {"selector_version": "1", "any_of": [{"all_of": [{"op": "any"}]}]}


def _key(*parts: Any) -> str:
    return "cli-" + hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:40]


class Reviewer:
    def __init__(self, project: str, **where: Any):
        self.project = project
        self.socket, self.token, _ = hooks.connection(project=project, **where)
        self.incarnation = request(self.socket, self.token, "context", {"project": project})[
            "incarnation"
        ]

    def call(self, action: str, args: dict[str, Any], key: str | None = None) -> dict[str, Any]:
        command: dict[str, Any] = {
            "version": 1,
            "incarnation": self.incarnation,
            "action": action,
            "args": {"project": self.project, **args},
        }
        if key:
            command["key"] = key
        return request(self.socket, self.token, "knowledge", {"command": command})

    def query(self, **args: Any) -> dict[str, Any]:
        return self.call("query", {"bindings": [{}], "diagnostic": True, **args})

    def revision(self, revision: str, *, excerpts: bool = True) -> dict[str, Any]:
        for kind in ("knowledge", "constraint"):
            found = self.query(revision=revision, kind=kind, excerpts=excerpts)["items"]
            if found:
                item: dict[str, Any] = found[0]
                return item
        raise LocalError("unavailable")

    def waiting(self, kind: str = "knowledge") -> list[dict[str, Any]]:
        """Proposals waiting for a decision, oldest first."""
        items: list[dict[str, Any]] = []
        before: int | None = None
        for _ in range(PAGES):
            args: dict[str, Any] = {"kind": kind}
            if before is not None:
                args["before_position"] = before
            page = self.query(**args)
            items += [i for i in page["items"] if i["state"] == "proposed"]
            if not page.get("truncated"):
                break
            before = page["next_before_position"]
        return sorted(items, key=lambda i: i["created_position"])

    def retirements(self) -> dict[str, Any]:
        return self.call("retirements", {})

    def decide(
        self, item: dict[str, Any], decision: str, reason: str, note: str | None
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "revision": item["revision"],
            "expected_version": item["family_version"],
            "decision": decision,
            "reason": reason,
            "breadth_reviewed": True,
        }
        if note:
            args["note"] = note
        key = _key("review", item["revision"], decision, note, item["family_version"])
        return self.call("review", args, key)

    def accept(
        self, revision: str, as_kind: str | None, note: str | None, effect: str
    ) -> dict[str, Any]:
        item = self.revision(revision, excerpts=False)
        if item["state"] != "proposed":
            raise LocalError("invalid_transition")
        kind = item["body"]["kind"]
        if as_kind == "knowledge" and kind == "constraint":
            raise LocalError("invalid_request")
        if as_kind != "constraint" or kind == "constraint":
            return {"accepted": self.decide(item, "accept", "reviewed_in_cli", note)}
        # A draft of knowledge becomes a mandatory constraint: file a constraint proposal
        # with the same words and evidence, accept it, and close the draft.
        body = {k: v for k, v in item["body"].items() if k != "executable"}
        digest = hashlib.sha256(body["text"].encode()).hexdigest()
        body.update(
            kind="constraint",
            authority="project",
            rule_key="rule-" + digest[:24],
            effect=effect,
            rule_value=body["text"][:256],
            provenance={
                **body.get("provenance", {}),
                "promoted_from": item["revision"],
                "via": "review_accept_as_constraint",
            },
        )
        filed = self.call(
            "propose",
            {
                "logical_key": "constraint-" + digest[:40],
                "expected_version": 0,
                "body": body,
            },
            _key("constraint", item["revision"]),
        )
        constraint = self.revision(filed["revision"], excerpts=False)
        decided = self.decide(constraint, "accept", "reviewed_in_cli", note)
        closed = self.decide(
            item, "reject", "accepted_as_constraint", f"Accepted as constraint {filed['revision']}"
        )
        return {"accepted": decided, "constraint": filed["revision"], "draft": closed}


def describe(selector: dict[str, Any]) -> str:
    if selector == ANY:
        return "everything in this project"
    return json.dumps(selector, sort_keys=True, separators=(",", ":"))


def render(item: dict[str, Any], *, full: bool) -> str:
    body = item["body"]
    provenance = body.get("provenance") or {}
    method = provenance.get("method") or "filed directly"
    support = provenance.get("support", len(body["evidence"]))
    lines = [
        f"{item['revision']}  {body['kind']}  {item['state']}  proposed by {item.get('proposed_by', '?')}",
        "  " + (body["text"] if full else body["text"].replace("\n", " ")[:200]),
        f"  method: {method} · supported by {support} observation(s)"
        + (f" · topic {provenance['topic']}" if provenance.get("topic") else ""),
    ]
    for span in item.get("evidence_text", []):
        lines.append(f"    - {span['observation']}: {span['text']!r}")
    if full:
        lines.append(f"  applies to: {describe(body['selector'])}")
        if body["kind"] == "constraint":
            lines.append(f"  rule: {body['effect']} ({body['rule_key']})")
        decision = item.get("decision")
        if decision:
            lines.append(
                f"  last decision: {decision['action']} by {decision.get('actor', '?')}"
                + (f" - {decision['note']}" if decision.get("note") else "")
                + (
                    f" (confirmed in chat; relayed by {decision['confirmation']['relayed_by']},"
                    f" proposed by {decision['confirmation']['proposed_by']})"
                    if decision.get("confirmation")
                    else ""
                )
            )
    return "\n".join(lines)


def connection_args(args: Any) -> dict[str, Any]:
    return {
        "socket": args.socket,
        "credential": Path(args.credential) if args.credential else None,
        "profile": args.profile,
        "config": args.config,
    }
