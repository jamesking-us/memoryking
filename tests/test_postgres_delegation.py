"""A delegated token is its issuer acting with a narrower scope; it never becomes the subject."""

import json
import unittest
from pathlib import Path

import test_postgres_store as base
from test_local_store import delivered, observation

from memoryking.adapters.handoff import Handoff
from memoryking.adapters.postgres_store import PostgresStore
from memoryking.adapters.working import Working
from memoryking.domain.local import LocalError

ROOT = Path(__file__).resolve().parents[1]
CAPS = ["read", "capture", "checkpoint", "handoff"]


class DelegationTests(unittest.TestCase):
    setUpClass = classmethod(base.PostgresStoreTests.setUpClass.__func__)
    tearDownClass = classmethod(base.PostgresStoreTests.tearDownClass.__func__)
    store = base.PostgresStoreTests.store

    def as_(self, credential):
        return PostgresStore(
            self.pool, self.root, self.control, self.tenant, credential, clock=self.clock
        )

    def setUp(self):
        base.PostgresStoreTests.setUp(self)
        with self.store() as store:
            self.issuer = store.grant(self.credential, "issuer", "demo", CAPS)
            self.subject = store.grant(self.credential, "subject", "demo", CAPS)
        self.n = 0
        with self.as_(self.issuer) as store:
            self.delegated = store.delegate("subject", "demo", CAPS, 600_000)

    def code(self, call):
        with self.assertRaises(LocalError) as caught:
            call()
        return caught.exception.code

    def denied(self, credential, capability, expected="denied"):
        with self.as_(credential) as store:
            with store.transaction() as decision:
                at = decision.evaluated_ms
                self.assertEqual(
                    expected, self.code(lambda: store._auth(credential, capability, "demo", at))
                )

    def work(self, credential, action, args, key=None):
        self.n += 1
        with self.as_(credential) as store:
            command = {
                "version": 1,
                "incarnation": store.incarnation,
                "action": action,
                "args": {"project": "demo", "task": "task", "run": None, **args},
            }
            if action == "save":
                command["key"] = key or f"w{self.n}"
            output = []
            Working(store).execute(credential, command).emit(output.append)
            return json.loads(output[0])

    def save(self, credential, readers, revision=0):
        body = json.loads((ROOT / "fixtures/contracts/checkpoint.json").read_text())
        return self.work(
            credential,
            "save",
            {"expected_revision": revision, "readers": readers, "checkpoint": body},
        )

    def resume(self, credential):
        return self.work(
            credential,
            "resume",
            {
                "checkpoint": None,
                "bindings": [{}],
                "max_output_tokens": 6000,
                "tokenizer": "cl100k_base",
            },
        )

    def handoff(self, credential, action, args):
        self.n += 1
        with self.as_(credential) as store:
            command = {
                "version": 1,
                "incarnation": store.incarnation,
                "action": action,
                "key": f"h{self.n}",
                "args": {"project": "demo", **args},
            }
            output = []
            Handoff(store).execute(credential, command).emit(output.append)
            return json.loads(output[0])

    def test_it_cannot_read_what_only_the_subject_may_read(self):
        with self.as_(self.subject) as store:
            store.capture(
                self.subject,
                store.namespace(self.subject, "demo"),
                "k1",
                observation(1, text="subject only secret", readers=["subject"]),
            )
            self.assertEqual(
                1, len(delivered(store.read(self.subject, "demo", query="secret"))["items"])
            )
        with self.as_(self.delegated) as store:
            self.assertEqual(
                [], delivered(store.read(self.delegated, "demo", query="secret"))["items"]
            )

    def test_it_cannot_overwrite_the_subjects_checkpoint_stream(self):
        self.save(self.subject, ["subject"])
        self.assertEqual(
            "checkpoint_writer_denied", self.code(lambda: self.save(self.delegated, ["issuer"], 1))
        )

    def test_it_cannot_receive_a_handoff_addressed_to_the_subject(self):
        saved = self.save(self.issuer, ["issuer", "subject"])
        prepared = self.handoff(
            self.issuer,
            "prepare",
            {
                "task": "task",
                "run": None,
                "checkpoint": saved["checkpoint"],
                "recipient": "subject",
                "role": "review",
                "stage": "verification",
                "bindings": [{}],
                "ttl_ms": 60000,
            },
        )
        view = {
            "handle": prepared["handle"],
            "max_output_tokens": 6000,
            "tokenizer": "cl100k_base",
        }
        self.assertEqual(
            "handoff_unavailable",
            self.code(lambda: self.handoff(self.delegated, "materialize", view)),
        )
        self.assertIn("handoff", self.handoff(self.subject, "materialize", view))

    def test_events_and_receipts_name_the_issuer_and_writes_are_the_issuers(self):
        with self.as_(self.delegated) as store:
            captured = store.capture(
                self.delegated, store.namespace(self.delegated, "demo"), "k1", observation(2)
            )
            delivered(store.read(self.delegated, "demo", query="durable"))
        saved = self.save(self.delegated, ["issuer"])
        self.assertEqual("issuer", self.resume(self.issuer)["checkpoint"]["writer"])
        self.assertEqual(
            "checkpoint_writer_denied", self.code(lambda: self.save(self.subject, ["subject"], 1))
        )
        with self.store() as store, store.transaction():
            actors = {
                row[0]
                for row in store.db.execute(
                    "SELECT actor FROM events WHERE tenant=? AND position>=?",
                    (self.tenant, captured["position"]),
                ).fetchall()
                if row[0] != "system"
            }
            receipts = {
                row[0]
                for row in store.db.execute(
                    "SELECT principal FROM receipts WHERE tenant=?", (self.tenant,)
                ).fetchall()
            }
        self.assertEqual({"issuer"}, actors)
        self.assertEqual({"issuer"}, receipts)
        self.assertTrue(saved["checkpoint"])

    def test_it_never_exceeds_the_issuers_current_grant(self):
        with self.as_(self.delegated) as store:
            with store.transaction() as decision:
                store._auth(self.delegated, "checkpoint", "demo", decision.evaluated_ms)
        with self.store() as store:
            store.grant(self.credential, "issuer", "demo", ["read"])
        with self.as_(self.delegated) as store:
            with store.transaction() as decision:
                store._auth(self.delegated, "read", "demo", decision.evaluated_ms)
        self.denied(self.delegated, "checkpoint")
        self.denied(self.delegated, "capture")
        with self.store() as store:
            store.revoke(self.credential, "issuer", "demo")
        self.denied(self.delegated, "read")

    def test_it_is_narrowed_to_the_named_project_and_capabilities(self):
        with self.as_(self.issuer) as store:
            narrow = store.delegate("subject", "demo", ["read"], 600_000)
        self.denied(narrow, "capture")
        self.denied(narrow, "checkpoint")
        with self.as_(narrow) as store:
            with store.transaction() as decision:
                store._auth(narrow, "read", "demo", decision.evaluated_ms)
                self.assertEqual(
                    "denied",
                    self.code(lambda: store._auth(narrow, "read", "other", decision.evaluated_ms)),
                )

    def test_rotating_the_issuer_or_the_subject_kills_it(self):
        with self.store() as store:
            store.grant(self.credential, "subject", "demo", CAPS, rotate=True)
        self.denied(self.delegated, "read", "credential_replaced")
        with self.as_(self.issuer) as store:
            fresh = store.delegate("subject", "demo", CAPS, 600_000)
        with self.as_(fresh) as store:
            with store.transaction() as decision:
                store._auth(fresh, "read", "demo", decision.evaluated_ms)
        with self.store() as store:
            store.grant(self.credential, "issuer", "demo", CAPS, rotate=True)
        self.denied(fresh, "read", "credential_replaced")

    def test_a_token_minted_under_the_old_rule_is_refused(self):
        chain = "delegation_" + "0" * 32
        with self.as_(self.issuer) as store:
            old = store._token(
                "credential",
                tenant=self.tenant,
                principal="subject",
                generation=0,
                delegation={
                    "id": chain,
                    "by": "issuer",
                    "by_generation": 0,
                    "project": "demo",
                    "capabilities": ["read"],
                    "expires": 2**50,
                },
                delegation_chain=[chain],
            )
        self.denied(old, "read", "delegation_superseded")


if __name__ == "__main__":
    unittest.main()
