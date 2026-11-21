"""The labelled tier: unreviewed drafts in a briefing only where an admin turned it on."""

import json
import tempfile
import threading
import unittest
from pathlib import Path

import test_knowledge as knowledge
import test_postgres_parity as pg

from memoryking.adapters.knowledge_store import Knowledge
from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
from memoryking.adapters.retrieval import Retrieval
from memoryking.adapters.unreviewed import Unreviewed
from memoryking.domain.local import LocalError
from memoryking.harness_context import AuthorizedConversation, LocalRecordingProvider
from memoryking.sdk import LocalClient

VOLATILE = ("receipt", "evaluated_ms", "deadline_ms", "snapshot")


class TierCases:
    def kn(self, action, args, key=None, credential=None):
        command = {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": action,
            "args": {"project": "p", **args},
        }
        if key:
            command["key"] = key
        if hasattr(self, "service"):
            return self.service(Knowledge, command, credential)
        output = []
        Knowledge(self.store).execute(credential or self.owner, command).emit(output.append)
        return json.loads(output[0])

    def tier(self, mode, credential=None):
        self.n += 1
        return self.kn(
            "unreviewed_tier", {"mode": mode}, key=f"tier{self.n}", credential=credential
        )

    def brief(self, query="timeout", credential=None, **extra):
        command = {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": "brief",
            "args": {
                "project": "p",
                "query": query,
                "bindings": [{}],
                "max_output_tokens": 6000,
                "tokenizer": "utf8-byte-estimate-v1",
                **extra,
            },
        }
        if hasattr(self, "service"):
            return self.service(Retrieval, command, credential)
        output = []
        Retrieval(self.store).execute(credential or self.owner, command).emit(output.append)
        return json.loads(output[0])

    def draft(
        self,
        text="Observed timeout is thirty seconds in staging.",
        method="agent_proposal_v1",
        key="d1",
        **prov,
    ):
        return self.propose(
            self.body(text, provenance={"method": method, **prov}),
            key=key,
        )

    def drafted(self, packet):
        return [g for g in packet["guidance"] if g["authority"] == "drafted_unreviewed"]

    def comparable(self, packet):
        return {k: v for k, v in packet.items() if k not in VOLATILE}

    def test_default_is_off_and_identical_to_a_store_that_never_had_the_tier(self):
        self.assertEqual(self.kn("unreviewed_tier_show", {})["enabled"], False)
        self.draft()
        before = self.brief()
        self.assertEqual(self.drafted(before), [])
        self.assertEqual(self.tier("on")["enabled"], True)
        self.assertEqual(self.kn("unreviewed_tier_show", {})["enabled"], True)
        self.assertEqual(len(self.drafted(self.brief())), 1)
        self.tier("off")
        after = self.brief()
        self.assertEqual(self.comparable(before), self.comparable(after))
        self.assertNotIn("unreviewed_drafts_included", after["warnings"])

    def test_a_draft_is_labelled_cited_and_never_a_constraint_or_attested(self):
        proposal = self.draft()
        self.tier("on")
        packet = self.brief()
        (item,) = self.drafted(packet)
        self.assertEqual(item["reference"], proposal["revision"])
        self.assertEqual(item["selection_reason"], "drafted_unreviewed")
        self.assertEqual(item["assurance"], "unreviewed_draft")
        self.assertEqual(item["attestations"], [])
        self.assertEqual(item["draft"]["method"], "agent_proposal_v1")
        self.assertEqual(item["draft"]["method_label"], "agent proposal")
        self.assertIsInstance(item["draft"]["age_ms"], int)
        self.assertEqual(item["draft"]["quotes"][0]["text"], self.raw[:240])
        self.assertEqual(packet["constraints"], [])
        self.assertIn("unreviewed_drafts_included", packet["warnings"])
        self.assertNotEqual(packet["status"], "no_relevant_memory")
        # Rendered in its own section after accepted guidance, never under "Must follow".
        text = self.brief(format="text")["text"]
        self.assertIn("## Unreviewed, drafted from evidence", text)
        self.assertNotIn("## Must follow", text)
        self.assertIn("agent proposal", text)
        # A caller that demands attested freshness gets no draft at all.
        self.assertEqual(self.drafted(self.brief(freshness="attested")), [])
        # Nor does a historical view, which cannot know what was unreviewed then.
        self.assertEqual(self.drafted(self.brief(valid_ms=self.store.clock.utc_ms())), [])

    def test_only_fact_like_kinds_from_known_methods_appear(self):
        self.draft(
            "Instruction: restore the timeout to thirty seconds always.",
            method="model_consolidation_v1",
            key="i",
            claim_kind="instruction",
        )
        self.draft(
            "Decision: keep the timeout at thirty seconds.",
            method="model_consolidation_v1",
            key="d",
            claim_kind="decision",
        )
        self.draft("Human proposal about the timeout without a method.", method="manual", key="h")
        self.draft(
            "Timeout update replacing an accepted claim.",
            method="model_consolidation_v1",
            key="r",
            claim_kind="fact",
            relation={"type": "updates", "claim": "rev_x"},
        )
        constraint = self.body(
            "Never change the timeout.",
            kind="constraint",
            authority="project",
            rule_key="timeout",
            effect="prohibit",
            rule_value="change",
            provenance={"method": "agent_proposal_v1"},
        )
        self.propose(constraint, key="c")
        self.tier("on")
        texts = [g["text"] for g in self.drafted(self.brief())]
        self.assertEqual(texts, ["Decision: keep the timeout at thirty seconds."])

    def test_a_decided_or_unverifiable_draft_leaves_the_next_briefing_at_once(self):
        first = self.draft(key="a")
        second = self.draft("Timeout was raised to sixty in the batch queue.", key="b")
        third = self.draft("Timeout is configured per attempt in the client.", key="c")
        self.tier("on")
        self.assertEqual(len(self.drafted(self.brief())), 3)
        self.review(first, decision="reject")
        refs = [g["reference"] for g in self.drafted(self.brief())]
        self.assertEqual(sorted(refs), sorted([second["revision"], third["revision"]]))
        # Accepting one turns it into accepted knowledge, no longer a draft.
        self.review(second)
        packet = self.brief()
        self.assertEqual([g["reference"] for g in self.drafted(packet)], [third["revision"]])
        accepted = [g for g in packet["guidance"] if g["authority"] == "accepted_knowledge"]
        self.assertEqual([g["reference"] for g in accepted], [second["revision"]])
        # Purging the evidence retires the claim built on it.
        self.store.purge(self.owner, self.obs["observation"])
        self.assertEqual(self.drafted(self.brief()), [])

    def test_a_reader_who_cannot_read_the_evidence_never_sees_the_draft(self):
        self.draft()
        self.tier("on")
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        self.assertEqual(len(self.drafted(self.brief(credential=reader))), 1)
        self.store.capture(
            self.owner,
            self.ns,
            "private",
            {
                "version": 1,
                "project": "p",
                "source": "test",
                "source_event": "private",
                "text": "The private timeout value for the billing queue is ninety seconds.",
                "readers": ["owner"],
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )
        private = self.store.db.execute(
            "SELECT id,blob FROM observations WHERE source_event='private'"
        ).fetchone()
        text = "The private timeout value for the billing queue is ninety seconds."
        self.propose(
            self.body(
                text,
                evidence=[
                    {
                        "observation": private["id"],
                        "start_byte": 0,
                        "end_byte": len(text.encode()),
                        "content_hash": private["blob"],
                    }
                ],
                provenance={"method": "agent_proposal_v1"},
            ),
            key="private",
        )
        mine = [g["text"] for g in self.drafted(self.brief(query="private timeout billing"))]
        theirs = [
            g["text"]
            for g in self.drafted(self.brief(query="private timeout billing", credential=reader))
        ]
        self.assertIn(text, mine)
        self.assertNotIn(text, theirs)

    def test_only_an_admin_decides_and_nothing_else_changes_health(self):
        reviewer = self.store.grant(self.owner, "rev", "p", ["read", "review", "capture"])
        with self.assertRaises(LocalError) as caught:
            self.tier("on", credential=reviewer)
        self.assertEqual(caught.exception.code, "denied")
        self.draft()
        before = self.kn("query", {"bindings": [{}], "diagnostic": True})
        self.tier("on")
        after = self.kn("query", {"bindings": [{}], "diagnostic": True})
        self.assertEqual(before["items"], after["items"])
        self.assertEqual([i["state"] for i in after["items"]], ["proposed"])
        self.assertEqual(
            self.kn("constraints", {"bindings": [{}]})["items"],
            [],
        )

    def test_a_briefing_with_drafts_is_untrusted_evidence_for_the_model(self):
        self.draft()
        self.tier("on")
        packet = self.brief()
        self.assertIn("untrusted_evidence", packet["warnings"])
        conversation = AuthorizedConversation(
            self.make_client(), LocalRecordingProvider(self.root / "rec")
        )
        conversation.add_memory(packet)
        self.assertEqual(conversation._messages[0]["role"], "user")
        self.assertEqual(conversation._proofs[0]["origin"], "memory")


class LocalTierTests(TierCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp

    def tearDown(self):
        self.doCleanups()  # stop any daemon before its store closes
        knowledge.KnowledgeTests.tearDown(self)

    propose = knowledge.KnowledgeTests.propose
    body = knowledge.KnowledgeTests.body
    review = knowledge.KnowledgeTests.review
    run_k = knowledge.KnowledgeTests.run_k

    def make_client(self):
        server = Server(self.store)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()

        def stop():
            server.shutdown()
            thread.join()
            server.server_close()

        self.addCleanup(stop)
        return LocalClient(server.path, self.control / "owner.credential", "p")

    def test_the_policy_survives_backup_and_restore(self):
        self.tier("on")
        with tempfile.TemporaryDirectory(prefix="mk-tier-", dir="/tmp") as folder:
            archive = Path(folder) / "archive"
            self.store.backup(archive)
            self.store.close()
            credential = LocalStore.restore(self.data, self.control, archive).read_text()
            self.store = LocalStore(self.data, self.control)
            self.owner = credential
            self.assertTrue(Unreviewed(self.store).enabled("main", "p"))
            self.assertFalse(Unreviewed(self.store).enabled("main", "other"))


class PostgresTierTests(TierCases, pg.NativeFixture):
    def make_client(self):
        from test_postgres_inference import NativeClient

        client = NativeClient(self)
        client.namespace = "sdk"
        return client


if __name__ == "__main__":
    unittest.main()
