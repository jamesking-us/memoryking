"""Memory about the user: a scope of its own, private to the user and the agents acting for them.

Store-level cases for both backends (the PostgreSQL parity suite adopts every `test_` here).
Daemon, CLI and bridge behavior is in test_user_scope_cli.
"""

import hashlib
import json
import unittest
from contextlib import contextmanager

import test_knowledge as knowledge_helpers
import test_retrieval as retrieval_helpers

from memoryking.adapters.projects import visible_projects
from memoryking.adapters.user_scope import UserScope

USER = "_user_alice"
VICTOR = "_user_victor"


class UserScopeTests(unittest.TestCase):
    tearDown = knowledge_helpers.KnowledgeTests.tearDown
    run_k = knowledge_helpers.KnowledgeTests.run_k
    body = knowledge_helpers.KnowledgeTests.body
    run_r = retrieval_helpers.RetrievalTests.run_r
    brief_args = retrieval_helpers.RetrievalTests.brief_args
    fails = retrieval_helpers.RetrievalTests.fails

    def setUp(self):
        knowledge_helpers.KnowledgeTests.setUp(self)
        self.arrange()

    @contextmanager
    def actor(self, credential):
        yield self.store

    def arrange(self):
        """The owner is the administrator and is nobody's user. Alice has agent-a acting for
        her; Victor has agent-b."""
        self.agent = self.store.grant(
            self.owner, "agent-a", "p", ["capture", "read"], for_user="alice"
        )
        self.victor_agent = self.store.grant(
            self.owner, "agent-b", "p", ["capture", "read"], for_user="victor"
        )
        everything = ["capture", "read", "export", "review", "policy"]
        self.alice = self.store.grant(self.owner, "alice", USER, everything)
        self.victor = self.store.grant(self.owner, "victor", VICTOR, everything)

    def user_op(self, credential, **args):
        with self.actor(credential) as store:
            return UserScope(store).execute(credential, args)

    def put(self, credential, project, text, event):
        raw = {
            "version": 1,
            "project": project,
            "source": "test",
            "source_event": event,
            "text": text,
            "readers": None,
            "classification": "internal",
            "observed_at_ms": 1,
        }
        with self.actor(credential) as store:
            done = store.capture(credential, store.namespace(credential, project), event, raw)
        span = {
            "observation": done["observation"],
            "start_byte": 0,
            "end_byte": len(text.encode()),
            "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        }
        return done["observation"], span

    def claim(self, credential, project, span, text, key="pref", **kw):
        return self.run_k(
            "propose",
            {
                "project": project,
                "logical_key": key,
                "expected_version": 0,
                "body": self.body(text=text, evidence=[span], **kw),
            },
            credential,
        )

    def accept(self, credential, project, proposed):
        return self.run_k(
            "review",
            {
                "project": project,
                "revision": proposed["revision"],
                "expected_version": proposed["family_version"],
                "decision": "accept",
                "reason": "reviewed_evidence",
                "breadth_reviewed": True,
            },
            credential,
        )

    def constraint(self, credential, project, span, text, value, key="pm"):
        return self.claim(
            credential,
            project,
            span,
            text,
            key=key,
            kind="constraint",
            authority="project",
            rule_key="package_manager",
            rule_value=value,
            effect="require",
        )

    def brief(self, credential, **kw):
        return self.run_r("brief", self.brief_args(query="pnpm packages", **kw), credential)

    def test_other_users_and_admins_cannot_reach_the_scope(self):
        obs, span = self.put(self.agent, USER, "The user wants pnpm for packages.", "u1")
        proposed = self.claim(self.alice, USER, span, "Use pnpm for packages.")
        self.accept(self.alice, USER, proposed)
        # The administrator holds `*` and `admin`; neither reaches a user scope.
        for call in (
            lambda: self.store.read(self.owner, USER),
            lambda: self.run_k("constraints", {"project": USER, "bindings": [{}]}),
            lambda: self.run_k("query", {"project": USER, "bindings": [{}]}),
            lambda: self.run_r("brief", self.brief_args(project=USER)),
            lambda: self.store.namespace(self.owner, USER),
        ):
            self.fails("denied", call)
        # Another user's agent, and another user, are refused the same way.
        for credential in (self.victor_agent, self.victor):
            self.fails("denied", lambda c=credential: self.store.read(c, USER))
            self.fails(
                "denied",
                lambda c=credential: self.run_k("query", {"project": USER, "bindings": [{}]}, c),
            )
        # Their briefings in the shared project carry none of it.
        packet = self.brief(self.victor_agent)
        self.assertEqual(packet["guidance"], [])
        self.assertNotIn(obs, json.dumps(packet))
        self.assertEqual(packet["constraints"], [])
        # A `*` holder's project list leaves the scope out; the user's own lists it.
        now = self.store.clock.utc_ms()
        with self.store.transaction():
            names = [i["project"] for i in visible_projects(self.store, self.owner, now)]
        self.assertNotIn(USER, names)
        with self.actor(self.alice) as store, store.transaction():
            listed = {i["project"]: i for i in visible_projects(store, self.alice, now)}
        self.assertEqual(listed[USER]["scope"], "user")
        self.assertEqual(listed[USER]["observations"], 1)

    def test_agent_writes_user_reviews_and_every_project_hears_it(self):
        obs, span = self.put(self.agent, USER, "The user wants pnpm for packages.", "u1")
        # Agents may propose but never decide: that is the user's.
        proposed = self.claim(self.agent, USER, span, "Use pnpm for packages.")
        self.fails("denied", lambda: self.accept(self.agent, USER, proposed))
        self.fails(
            "denied",
            lambda: self.constraint(self.agent, USER, span, "Always pnpm.", "pnpm"),
        )
        # Until reviewed, a proposal is not in any briefing.
        self.assertNotIn(
            proposed["revision"], [g["reference"] for g in self.brief(self.agent)["guidance"]]
        )
        self.accept(self.alice, USER, proposed)
        rule = self.constraint(self.alice, USER, span, "Always use pnpm.", "pnpm")
        self.accept(self.alice, USER, rule)
        packet = self.brief(self.agent)
        self.assertEqual(packet["status"], "partial", packet)
        mandatory = [c for c in packet["constraints"] if c["rule_key"] == "package_manager"]
        self.assertEqual(len(mandatory), 1)
        self.assertEqual(mandatory[0]["scope"], "user")
        scopes = {g["reference"]: g.get("scope") for g in packet["guidance"]}
        self.assertEqual(scopes[proposed["revision"]], "user")
        self.assertEqual(scopes[obs], "user")
        # The same, from a different project the agent also reads.
        self.store.grant(self.owner, "agent-a", "q", ["capture", "read"], for_user="alice")
        other = self.run_r("brief", self.brief_args(project="q", query="pnpm"), self.agent)
        self.assertEqual(
            [c["scope"] for c in other["constraints"] if c["rule_key"] == "package_manager"],
            ["user"],
        )
        # The receipt depends on the user's observation: purging it retires the briefing.
        receipt = packet["receipt"]
        self.run_r("replay", {"project": "p", "receipt": receipt}, self.agent, raw=True)
        self.user_op(self.alice, action="purge")
        self.fails(
            "replay_purged",
            lambda: self.run_r(
                "replay", {"project": "p", "receipt": receipt}, self.agent, raw=True
            ),
        )
        self.assertEqual(
            [
                c
                for c in self.brief(self.agent)["constraints"]
                if c["rule_key"] == "package_manager"
            ],
            [],
        )

    def test_user_constraint_and_project_constraint_conflict_never_silently_wins(self):
        _, mine = self.put(self.alice, USER, "The user wants pnpm.", "u1")
        rule = self.constraint(self.alice, USER, mine, "Always use pnpm.", "pnpm")
        self.accept(self.alice, USER, rule)
        before = self.brief(self.agent)
        self.assertEqual(before["status"], "partial")
        project_rule = self.constraint(self.owner, "p", self.span, "This project uses npm.", "npm")
        self.accept(self.owner, "p", project_rule)
        blocked = self.brief(self.agent)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["reason"], "unresolved_conflict")
        self.assertEqual(len(blocked["conflicts"]), 1)
        # Another user's agent sees only the project's rule, with no conflict.
        theirs = self.brief(self.victor_agent)
        self.assertEqual(theirs["status"], "partial")
        self.assertEqual([c["text"] for c in theirs["constraints"]], ["This project uses npm."])

    def test_the_scope_keeps_its_own_evidence(self):
        _, mine = self.put(self.alice, USER, "The user prefers small commits.", "u1")
        # An agent that reads both scopes still cannot move evidence across the boundary.
        self.fails("user_evidence_scope", lambda: self.claim(self.agent, "p", mine, "Small."))
        self.fails(
            "user_evidence_scope", lambda: self.claim(self.agent, USER, self.span, "timeout=30")
        )

    def test_linking_follows_the_scope_rules(self):
        # An agent acts for one user.
        self.fails(
            "user_link_conflict",
            lambda: self.store.grant(self.owner, "agent-a", VICTOR, ["capture", "read"]),
        )
        # Agents get capture, read and export; reviewing stays with the user.
        self.fails(
            "invalid_capabilities",
            lambda: self.store.grant(self.owner, "agent-c", USER, ["capture", "review"]),
        )
        # An agent cannot link anyone; the user can link their own agents.
        self.fails("denied", lambda: self.store.grant(self.agent, "agent-c", USER, ["read"]))
        with self.actor(self.victor) as store:
            store.grant(self.victor, "agent-d", VICTOR, ["capture", "read"])
        shown = self.user_op(self.victor, action="show")
        self.assertEqual(
            sorted(a["principal"] for a in shown["agents"]), ["agent-b", "agent-d", "victor"]
        )
        # The user ends a link and the agent's access to the scope ends with it.
        self.put(self.victor_agent, VICTOR, "Victor writes in Welsh.", "v1")
        with self.actor(self.victor) as store:
            store.revoke(self.victor, "agent-b", VICTOR)
        self.fails("denied", lambda: self.put(self.victor_agent, VICTOR, "again", "v2"))

    def test_a_link_carries_only_what_the_credential_was_issued_with_and_ends_with_it(self):
        reader = self.store.grant(self.owner, "agent-r", "p", ["read"], for_user="alice")
        self.fails("denied", lambda: self.put(reader, USER, "not allowed", "r1"))
        # A credential without capture or read acts for nobody.
        worker = self.store.grant(self.owner, "worker", "p", ["jobs"], for_user="alice")
        self.fails("denied", lambda: self.user_op(worker, action="scope"))
        # Ending the agent's last project grant ends its link and retires its credential.
        self.store.revoke(self.owner, "agent-r", "p")
        self.fails("denied", lambda: self.user_op(reader, action="scope"))
        shown = self.user_op(self.alice, action="show")
        self.assertNotIn("agent-r", [a["principal"] for a in shown["agents"]])

    def test_session_start_carries_standing_user_claims(self):
        _, mine = self.put(self.alice, USER, "The user replies in British English.", "u1")
        self.accept(
            self.alice, USER, self.claim(self.alice, USER, mine, "Reply in British English.")
        )
        args = {
            "project": "p",
            "bindings": [{}],
            "max_output_tokens": 6000,
            "tokenizer": "cl100k_base",
        }
        packet = self.run_r("start", args, self.agent)
        item = next(g for g in packet["guidance"] if g["text"] == "Reply in British English.")
        self.assertEqual((item["scope"], item["selection_reason"]), ("user", "user_memory"))
        self.assertEqual([g for g in self.run_r("start", args, self.victor_agent)["guidance"]], [])

    def test_administrators_purge_without_reading_and_counts_show(self):
        self.put(self.alice, USER, "The user lives in Cardiff.", "u1")
        self.assertEqual(self.store.status(self.owner, "p")["user_scopes"]["observations"], 1)
        self.fails("denied", lambda: self.user_op(self.owner, action="show", user="alice"))
        # Another user cannot purge this scope.
        self.fails("denied", lambda: self.user_op(self.victor, action="purge", user="alice"))
        done = self.user_op(self.owner, action="purge", user="alice")
        self.assertEqual((done["purged"], done["remaining"]), (1, 0))
        self.assertEqual(self.store.status(self.owner, "p")["user_scopes"]["observations"], 0)
        self.assertEqual(self.user_op(self.alice, action="show")["observations"], 0)

    def test_the_owner_is_the_default_user(self):
        scope = self.user_op(self.owner, action="ensure")
        self.assertEqual(
            (scope["user"], scope["project"], scope["owner"]), ("owner", "_user_owner", True)
        )
        helper = self.store.grant(self.owner, "agent-e", "p", ["capture", "read"], for_user="@self")
        self.put(helper, "_user_owner", "The owner likes small commits.", "o1")
        self.assertEqual(self.user_op(self.owner, action="show")["observations"], 1)
        self.assertEqual(self.user_op(helper, action="scope")["project"], "_user_owner")
        self.fails("denied", lambda: self.store.read(self.agent, "_user_owner"))
        # An agent that holds no user link gets no scope.
        loose = self.store.grant(self.owner, "agent-f", "p", ["capture", "read"])
        self.fails("denied", lambda: self.user_op(loose, action="scope"))


if __name__ == "__main__":
    unittest.main()
