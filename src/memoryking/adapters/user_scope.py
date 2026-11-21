"""Memory about one user, kept in a scope of its own and shared with that user's agents only.

A user scope is the reserved project `_user_<principal>`. It is an ordinary project in every
way the store already knows (observations, claims, constraints, receipts, holds, purge,
backups) with one difference, decided in `StoreCore._auth`: a `*` grant never reaches it, so
only the user and the agents recorded as acting for them can read it. An agent acts for a
user when it holds a grant on that scope, a journaled record like any grant; an
administrator can create or end that record and can purge the scope, but cannot read it.

This module answers the four questions the `user` operation asks: which scope is mine
(`scope`), make the owner's own (`ensure`), what does it hold and who acts for me (`show`),
and erase it (`purge`).
"""

from __future__ import annotations

import json
from typing import Any

from memoryking.application.store_core import StoreCore
from memoryking.domain.local import (
    USER_OWNER_CAPABILITIES,
    LocalError,
    is_user_project,
    project_owner,
    temporal_valid,
    user_project,
)

PURGE_BATCH = 64


class UserScope:
    def __init__(self, store: StoreCore):
        self.s, self.db = store, store.db

    def execute(self, credential: str, args: dict[str, Any]) -> dict[str, Any]:
        action = args["action"]
        with self.s.transaction() as decision:
            at = decision.evaluated_ms
            if action == "purge":
                result = self.purge(credential, args, at)
            elif action == "ensure":
                result = self.ensure(credential, at)
            else:
                who, project = self._project(credential, args.get("user"), at)
                result = self.scope(who, project, at)
                if action == "show":
                    result.update(self.show(who, project, at))
        self.s._expose()
        if action == "purge":
            self.s.gc()
        return result

    def _project(self, credential: str, user: str | None, at: int) -> tuple[dict[str, Any], str]:
        """The scope the caller reads: its own, or the named one when it holds a grant there."""
        identity = self.s._decode(credential, "credential")
        project = user_project(user) if user is not None else None
        if project is None:
            project = self.s.user_scope(identity, "read", at) or self.s.user_scope(
                identity, "capture", at
            )
        if project is None:
            raise LocalError("denied")
        # The credential must be current and hold a capability in exactly this scope.
        try:
            return self.s._auth(credential, "read", project, at), project
        except LocalError as exc:
            if exc.code != "denied":
                raise
            return self.s._auth(credential, "capture", project, at), project

    def scope(self, who: dict[str, Any], project: str, at: int) -> dict[str, Any]:
        capabilities: set[str] = set()
        for row in self.db.execute(
            "SELECT capabilities,starts,expires FROM grants WHERE tenant=? AND principal=? AND project=?",
            (who["tenant"], who["principal"], project),
        ).fetchall():
            if temporal_valid(at, row["starts"], row["expires"]):
                capabilities.update(self._caps(row["capabilities"]))
        owner = project_owner(project)
        return {
            "user": owner,
            "project": project,
            "owner": who["principal"] == owner,
            "capabilities": sorted(capabilities),
        }

    @staticmethod
    def _caps(raw: Any) -> list[str]:
        return list(json.loads(raw) if isinstance(raw, str) else raw)

    def ensure(self, credential: str, at: int) -> dict[str, Any]:
        """Give the caller their own scope when they are an administrator without one.

        The local owner is the default user. Nobody else's scope is created here, and a
        principal that already has a user scope (their own or one they act for) keeps it."""
        identity = self.s._decode(credential, "credential")
        who = self.s._auth(credential, "admin", "*", at)
        project = user_project(who["principal"])
        if (
            self.s.user_scope(who, "read", at) is None
            and not self.db.execute(
                "SELECT 1 FROM grants WHERE tenant=? AND principal=? AND substr(project,1,6)=?",
                (identity["tenant"], who["principal"], user_project("")),
            ).fetchone()
        ):
            record = self.s._journal(
                {
                    "kind": "grant",
                    "actor": who["principal"],
                    "tenant": who["tenant"],
                    "principal": who["principal"],
                    "project": project,
                    "capabilities": sorted(USER_OWNER_CAPABILITIES),
                    "starts": None,
                    "expires": None,
                }
            )
            self.s._apply(record, at)
        return self.scope(who, self.s.user_scope(who, "read", at) or project, at)

    def show(self, who: dict[str, Any], project: str, at: int) -> dict[str, Any]:
        """What the scope holds and who acts for the user. Counts and names, never content."""
        tenant = who["tenant"]

        def count(statement: str) -> int:
            return int(self.db.execute(statement, (tenant, project)).fetchone()[0])

        agents = []
        for row in self.db.execute(
            "SELECT principal,capabilities,starts,expires FROM grants WHERE tenant=? AND project=? ORDER BY principal",
            (tenant, project),
        ).fetchall():
            if not temporal_valid(at, row["starts"], row["expires"]):
                continue
            agents.append(
                {
                    "principal": row["principal"],
                    "capabilities": sorted(self._caps(row["capabilities"])),
                    "expires": row["expires"],
                }
            )
        return {
            "observations": count(
                "SELECT count(*) FROM observations WHERE tenant=? AND project=? AND deleted=0"
            ),
            "held": count(
                "SELECT count(*) FROM observation_holds WHERE tenant=? AND project=? AND state='held'"
            ),
            "claims": count(
                "SELECT count(*) FROM revisions r JOIN families f ON r.tenant=f.tenant AND r.family=f.id WHERE r.tenant=? AND f.project=? AND r.purged=0 AND r.kind='knowledge'"
            ),
            "constraints": count(
                "SELECT count(*) FROM revisions r JOIN families f ON r.tenant=f.tenant AND r.family=f.id WHERE r.tenant=? AND f.project=? AND r.purged=0 AND r.kind='constraint'"
            ),
            "agents": agents,
        }

    def purge(self, credential: str, args: dict[str, Any], at: int) -> dict[str, Any]:
        """Erase one batch of the scope's observations with everything built from them.

        The user purges their own scope; an administrator may purge any scope for compliance
        without being able to read it. Each observation goes through the same journaled purge
        as any other, so claims, briefings and receipts that used it are retired too. Repeat
        until `remaining` is zero."""
        identity = self.s._decode(credential, "credential")
        user = args.get("user")
        if user is None:
            scope = self.s.user_scope(identity, "policy", at)
            if scope is None:
                raise LocalError("denied")
            user = project_owner(scope)
        project = user_project(user)
        who = self.s.user_authority(credential, project, at)
        purged = 0
        for row in self.db.execute(
            "SELECT id FROM observations WHERE tenant=? AND project=? AND deleted=0 ORDER BY position LIMIT ?",
            (who["tenant"], project, PURGE_BATCH),
        ).fetchall():
            self.s._apply(
                self.s._journal(
                    {
                        "kind": "purge",
                        "actor": who["principal"],
                        "tenant": who["tenant"],
                        "observation": row["id"],
                    }
                ),
                at,
            )
            purged += 1
        # A purged accepted constraint stays behind as a blocking tombstone (a briefing must not
        # silently lose mandatory policy). The user erasing their scope is that decision.
        from memoryking.adapters.knowledge_store import Knowledge

        knowledge = Knowledge(self.s)
        for row in self.db.execute(
            "SELECT r.id,r.body_hash FROM revisions r JOIN families f ON r.tenant=f.tenant AND r.family=f.id WHERE r.tenant=? AND f.project=? AND r.purged=1",
            (who["tenant"], project),
        ).fetchall():
            if knowledge._state(who["tenant"], row["id"], 9_007_199_254_740_991)[0] == "accepted":
                self.s._apply(
                    self.s._journal(
                        {
                            "kind": "knowledge",
                            "tenant": who["tenant"],
                            "actor": who["principal"],
                            "revision": row["id"],
                            "decision": "deprecate",
                            "reason": "user_scope_purged",
                            "replacement": None,
                            "expected_hash": row["body_hash"],
                        }
                    ),
                    at,
                )
        remaining = int(
            self.db.execute(
                "SELECT count(*) FROM observations WHERE tenant=? AND project=? AND deleted=0",
                (who["tenant"], project),
            ).fetchone()[0]
        )
        return {"user": user, "project": project, "purged": purged, "remaining": remaining}


__all__ = ["UserScope", "is_user_project"]
