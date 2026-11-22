"""Corrections: replacements, retirement requests and pins.

A replacement capture names the earlier observation it supersedes. The old record stays as
evidence (never altered, never deleted), but a briefing prefers the replacement: the old
one no longer appears beside it, and appears alone only when the replacement is not
visible to the reader (unreadable, held or purged).

An agent cannot purge. It files a retirement request for a record it can read; the record
is marked "retirement requested" for other readers and hidden from the requester's own
briefings. An admin approves (a journaled purge with the usual dependency follow-through)
or a reviewer or admin dismisses it.

A pin keeps an accepted claim or an observation in the session-start briefing. Pins and
dismissals are policy decisions: they go through the signed control journal, so a restore
replays them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from memoryking.application.storage import upsert
from memoryking.domain.local import LocalError, is_user_project

if TYPE_CHECKING:
    from memoryking.application.store_core import StoreCore

CHUNK = 400
PREVIEW = 160


def _chunks(ids: list[str]) -> list[list[str]]:
    return [ids[start : start + CHUNK] for start in range(0, len(ids), CHUNK)]


class Corrections:
    def __init__(self, store: StoreCore):
        self.s, self.db = store, store.db

    # --- Supersession ----------------------------------------------------------------

    def earlier(self, who: dict[str, Any], request: dict[str, Any], at: int) -> None:
        """Refuse a capture whose `supersedes` is not a record the caller may read here."""
        row = self.db.execute(
            "SELECT * FROM observations WHERE tenant=? AND id=? AND project=?",
            (who["tenant"], request["supersedes"], request["project"]),
        ).fetchone()
        if row is None or not self.s._visible(who, row, at):
            raise LocalError("unavailable")

    def link(
        self, who: dict[str, Any], request: dict[str, Any], replacement: str, position: int
    ) -> None:
        self.db.execute(
            "INSERT INTO supersessions VALUES (?,?,?,?,?,?)",
            (
                who["tenant"],
                replacement,
                request["supersedes"],
                request["project"],
                who["principal"],
                position,
            ),
        )

    def screen(
        self, who: dict[str, Any], ids: list[str], recorded: int, at: int
    ) -> tuple[set[str], set[str]]:
        """(hidden, marked) among observation ids, for one reader.

        Hidden: a replacement this reader can see (recorded by `recorded`) exists, or the
        reader asked for the record's retirement. Marked: someone else asked for it."""
        hidden: set[str] = set()
        marked: set[str] = set()
        tenant = who["tenant"]
        for chunk in _chunks(ids):
            marks = ",".join("?" for _ in chunk)
            for link in self.db.execute(
                "SELECT s.superseded,o.* FROM supersessions s JOIN observations o ON o.tenant=s.tenant AND o.id=s.replacement WHERE s.tenant=? AND s.superseded IN ("
                + marks
                + ") AND o.position<=?",
                (tenant, *chunk, recorded),
            ).fetchall():
                if link["superseded"] not in hidden and self.s._visible(who, link, at):
                    hidden.add(link["superseded"])
            for ask in self.db.execute(
                "SELECT observation,requester FROM retirement_requests WHERE tenant=? AND state='open' AND observation IN ("
                + marks
                + ")",
                (tenant, *chunk),
            ).fetchall():
                if ask["requester"] == who["principal"]:
                    hidden.add(ask["observation"])
                else:
                    marked.add(ask["observation"])
        return hidden, marked - hidden

    # --- Retirement requests ---------------------------------------------------------

    def request(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        from memoryking.adapters.local_store import uid

        tenant = who["tenant"]
        row = self.db.execute(
            "SELECT * FROM observations WHERE tenant=? AND id=? AND project=?",
            (tenant, args["observation"], args["project"]),
        ).fetchone()
        if row is None or not self.s._visible(who, row, at):
            raise LocalError("unavailable")
        open_one = self.db.execute(
            "SELECT id FROM retirement_requests WHERE tenant=? AND observation=? AND requester=? AND state='open'",
            (tenant, row["id"], who["principal"]),
        ).fetchone()
        if open_one is not None:
            return {"request": open_one["id"], "observation": row["id"], "state": "open"}
        ident = uid("ret")
        self.db.execute(
            "INSERT INTO retirement_requests VALUES (?,?,?,?,?,?,?,?,NULL,NULL)",
            (tenant, ident, args["project"], row["id"], who["principal"], args["note"], "open", at),
        )
        position = self.s._event(tenant, "retirement_requested", row["id"], at, who["principal"])
        self.s._enqueue(tenant, position, None, who["principal"])
        return {"request": ident, "observation": row["id"], "state": "open"}

    def listing(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        """Open requests, oldest first, with a bounded preview of what would be erased."""
        from memoryking.adapters.retrieval import Retrieval

        items = []
        rows = self.db.execute(
            "SELECT * FROM retirement_requests WHERE tenant=? AND project=? AND state='open' ORDER BY requested,id LIMIT 65",
            (who["tenant"], args["project"]),
        ).fetchall()
        for row in rows[:64]:
            record = self.db.execute(
                "SELECT * FROM observations WHERE tenant=? AND id=?",
                (who["tenant"], row["observation"]),
            ).fetchone()
            preview = None
            source = None
            if record is not None and self.s._visible(who, record, at):
                text = Retrieval(self.s)._blob(record).decode("utf-8", "replace")
                preview = text[:PREVIEW] + ("…" if len(text) > PREVIEW else "")
                source = record["source"]
            items.append(
                {
                    "request": row["id"],
                    "observation": row["observation"],
                    "requested_by": row["requester"],
                    "requested_ms": row["requested"],
                    "note": row["note"],
                    "source": source,
                    "preview": preview,
                }
            )
        return {"items": items, "truncated": len(rows) > 64}

    def decide(
        self, credential: str, who: dict[str, Any], args: dict[str, Any], at: int
    ) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM retirement_requests WHERE tenant=? AND id=? AND project=?",
            (who["tenant"], args["request"], args["project"]),
        ).fetchone()
        if row is None:
            raise LocalError("unavailable")
        if row["state"] != "open":
            raise LocalError("invalid_transition")
        if args["decision"] == "approve":
            # Purge stays an admin action: a reviewer may dismiss but not approve.
            if is_user_project(args["project"]):
                self.s.user_authority(credential, args["project"], at)
            else:
                self.s._auth(credential, "admin", "*", at)
            self.s._apply(
                self.s._journal(
                    {
                        "kind": "purge",
                        "tenant": who["tenant"],
                        "actor": who["principal"],
                        "observation": row["observation"],
                    }
                ),
                at,
            )
            return {"request": row["id"], "state": "approved", "observation": row["observation"]}
        self.s._apply(
            self.s._journal(
                {
                    "kind": "retirement",
                    "tenant": who["tenant"],
                    "actor": who["principal"],
                    "request": row["id"],
                    "reason": args["reason"],
                }
            ),
            at,
        )
        return {"request": row["id"], "state": "dismissed", "observation": row["observation"]}

    # --- Pins ------------------------------------------------------------------------

    def pin(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        project, kind, target = args["project"], args["target_kind"], args["target"]
        if args["mode"] == "add":
            if kind == "observation":
                row = self.db.execute(
                    "SELECT * FROM observations WHERE tenant=? AND id=? AND project=?",
                    (who["tenant"], target, project),
                ).fetchone()
                if row is None or not self.s._visible(who, row, at):
                    raise LocalError("unavailable")
            else:
                target = self.family(who["tenant"], project, target)
                if not self.accepted(who["tenant"], target):
                    raise LocalError("invalid_transition")
        elif kind == "claim":
            target = self.family(who["tenant"], project, target)
        self.s._apply(
            self.s._journal(
                {
                    "kind": "pin",
                    "tenant": who["tenant"],
                    "actor": who["principal"],
                    "project": project,
                    "pin_kind": kind,
                    "target": target,
                    "mode": args["mode"],
                }
            ),
            at,
        )
        return {"project": project, "target_kind": kind, "target": target, "mode": args["mode"]}

    def family(self, tenant: str, project: str, ident: str) -> str:
        """A claim is pinned by family, so a replacement revision keeps the pin."""
        row = (
            self.db.execute(
                "SELECT f.id FROM revisions r JOIN families f ON f.tenant=r.tenant AND f.id=r.family WHERE r.tenant=? AND r.id=? AND f.project=?",
                (tenant, ident, project),
            ).fetchone()
            or self.db.execute(
                "SELECT id FROM families WHERE tenant=? AND id=? AND project=?",
                (tenant, ident, project),
            ).fetchone()
        )
        if row is None:
            raise LocalError("unavailable")
        return str(row[0])

    def accepted(self, tenant: str, family: str) -> bool:
        from memoryking.adapters.knowledge_store import Knowledge

        frontier = 9_007_199_254_740_991
        k = Knowledge(self.s)
        return any(
            k._state(tenant, r["id"], frontier)[0] == "accepted"
            for r in self.db.execute(
                "SELECT id FROM revisions WHERE tenant=? AND family=? AND kind!='procedure'",
                (tenant, family),
            ).fetchall()
        )

    def pinned(self, tenant: str, project: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT kind,target,pinned_by,pinned FROM pins WHERE tenant=? AND project=? ORDER BY pinned,target",
                (tenant, project),
            ).fetchall()
        ]

    def pins(self, who: dict[str, Any], args: dict[str, Any], at: int) -> dict[str, Any]:
        items = []
        for pin in self.pinned(who["tenant"], args["project"]):
            items.append(
                {
                    "target_kind": pin["kind"],
                    "target": pin["target"],
                    "pinned_by": pin["pinned_by"],
                    "pinned_ms": pin["pinned"],
                }
            )
        return {"items": items}

    # --- Journal replay --------------------------------------------------------------

    def apply_policy(self, action: dict[str, Any], at: int) -> None:
        tenant = action["tenant"]
        if action["kind"] == "pin":
            if action["mode"] == "add":
                self.db.execute(
                    upsert("pins"),
                    (
                        tenant,
                        action["project"],
                        action["pin_kind"],
                        action["target"],
                        action["actor"],
                        at,
                    ),
                )
            else:
                self.db.execute(
                    "DELETE FROM pins WHERE tenant=? AND project=? AND kind=? AND target=?",
                    (tenant, action["project"], action["pin_kind"], action["target"]),
                )
        elif action["kind"] == "retirement":
            self.db.execute(
                "UPDATE retirement_requests SET state='dismissed',decided_by=?,decided=? WHERE tenant=? AND id=? AND state='open'",
                (action["actor"], at, tenant, action["request"]),
            )
        else:
            raise LocalError("journal_invalid")

    def purged(self, tenant: str, observation: str, actor: str, at: int) -> None:
        """What a purge of `observation` removes here: links, the request texts, its pins."""
        self.db.execute(
            "DELETE FROM supersessions WHERE tenant=? AND (replacement=? OR superseded=?)",
            (tenant, observation, observation),
        )
        self.db.execute(
            "UPDATE retirement_requests SET state='approved',decided_by=?,decided=? WHERE tenant=? AND observation=? AND state='open'",
            (actor, at, tenant, observation),
        )
        self.db.execute(
            "UPDATE retirement_requests SET note='' WHERE tenant=? AND observation=?",
            (tenant, observation),
        )
        self.db.execute(
            "DELETE FROM pins WHERE tenant=? AND kind='observation' AND target=?",
            (tenant, observation),
        )
