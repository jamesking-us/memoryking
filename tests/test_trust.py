"""Source trust on both backends: untrusted captures are held, invisible, then released."""

import json
import tempfile
import unittest
from pathlib import Path

import test_knowledge as knowledge
import test_postgres_parity as pg

from memoryking.adapters.inspection import Inspection
from memoryking.adapters.local_store import LocalStore
from memoryking.adapters.retrieval import Retrieval
from memoryking.adapters.trust import Trust
from memoryking.domain.local import LocalError


def observation(source, event, text):
    return {
        "version": 1,
        "project": "p",
        "source": source,
        "source_event": event,
        "text": text,
        "readers": None,
        "classification": "internal",
        "observed_at_ms": 1,
    }


class TrustCases:
    def inspect(self, action, key=None, credential=None, **args):
        command = {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": action,
            "args": {"project": "p", **args},
        }
        if key is not None:
            command["key"] = key
        output = []
        Inspection(self.store).execute(credential or self.owner, command).emit(output.append)
        return json.loads(output[0])

    def tenant_name(self):
        return getattr(self, "tenant", "main")

    def read(self, **args):
        output = []
        self.store.read(self.owner, "p", **args).emit(output.append)
        return json.loads(output[0])

    def brief_refs(self, query):
        output = []
        Retrieval(self.store).execute(
            self.owner,
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": "brief",
                "args": {
                    "project": "p",
                    "query": query,
                    "bindings": [{}],
                    "max_output_tokens": 4000,
                    "tokenizer": "utf8-byte-estimate-v1",
                },
            },
        ).emit(output.append)
        return [g["reference"] for g in json.loads(output[0])["guidance"]]

    def test_untrusted_source_is_held_invisible_and_released_by_an_admin(self):
        rule = self.inspect("trust", key="rule-1", source="scraper-*", level="untrusted")
        self.assertEqual(rule["status"], "recorded")
        self.assertEqual(
            self.inspect("trust", key="rule-1", source="scraper-*", level="untrusted")["replayed"],
            True,
        )
        listed = self.inspect("trust_list")["rules"]
        self.assertEqual([(r["source"], r["level"]) for r in listed], [("scraper-*", "untrusted")])
        held = self.store.capture(
            self.owner,
            self.ns,
            "scrape-1",
            observation("scraper-web", "scrape-1", "Zebra retry hint from a web page"),
        )
        self.assertTrue(held["held"])
        clean = self.store.capture(
            self.owner,
            self.ns,
            "note-1",
            observation("notes", "note-1", "Zebra retry hint from the team"),
        )
        self.assertFalse(clean["held"])
        # Invisible to reads, briefings and the graph; present for the admin to review.
        self.assertEqual(self.brief_refs("zebra retry"), [clean["observation"]])
        read = self.read(query="zebra")
        self.assertEqual([i["id"] for i in read["items"]], [clean["observation"]])
        with self.assertRaises(LocalError):
            self.read(observation=held["observation"])
        queue = self.inspect("held")["held"]
        self.assertEqual([q["observation"] for q in queue], [held["observation"]])
        self.assertEqual(queue[0]["reason"], "untrusted_source:scraper-web")
        self.assertIn("web page", queue[0]["preview"])
        # A reader cannot see the queue or set policy.
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        with self.assertRaisesRegex(LocalError, "denied"):
            self.inspect("held", credential=reader)
        released = self.inspect("release", key="rel-1", observation=held["observation"])
        self.assertEqual(released["status"], "released")
        self.assertTrue(
            self.inspect("release", key="rel-1", observation=held["observation"])["replayed"]
        )
        with self.assertRaisesRegex(LocalError, "hold_unavailable"):
            self.inspect("release", key="rel-2", observation=held["observation"])
        self.assertEqual(
            set(self.brief_refs("zebra retry")), {clean["observation"], held["observation"]}
        )
        self.assertEqual(self.inspect("held")["held"], [])
        # Lifting the rule stops holding; the specific source still beats the pattern.
        self.inspect("trust", key="rule-2", source="scraper-web", level="trusted")
        again = self.store.capture(
            self.owner, self.ns, "scrape-2", observation("scraper-web", "scrape-2", "Second scrape")
        )
        self.assertFalse(again["held"])
        other = self.store.capture(
            self.owner, self.ns, "scrape-3", observation("scraper-feed", "scrape-3", "Feed scrape")
        )
        self.assertTrue(other["held"])
        with self.assertRaisesRegex(LocalError, "invalid_request"):
            self.inspect("trust", key="bad", source="scraper-*", level="maybe")

    def test_wildcard_project_rule_applies_everywhere_and_purge_still_works(self):
        self.inspect("trust", key="rule-all", project="*", source="hook-*", level="untrusted")
        self.assertEqual(
            Trust(self.store).level(self.tenant_name(), "p", "hook-compact"), "untrusted"
        )
        self.assertEqual(Trust(self.store).level(self.tenant_name(), "p", "cli"), "trusted")
        held = self.store.capture(
            self.owner, self.ns, "h1", observation("hook-compact", "h1", "transcript tail")
        )
        self.assertTrue(held["held"])
        self.store.purge(self.owner, held["observation"])
        self.assertEqual(self.inspect("held")["held"], [])


class LocalTrustTests(TrustCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown

    def test_rules_and_releases_survive_backup_and_restore(self):
        self.inspect("trust", key="rule-1", source="scraper-*", level="untrusted")
        held = self.store.capture(
            self.owner, self.ns, "s1", observation("scraper-a", "s1", "held before backup")
        )
        kept = self.store.capture(
            self.owner, self.ns, "s2", observation("scraper-b", "s2", "released before backup")
        )
        self.inspect("release", key="rel", observation=kept["observation"])
        with tempfile.TemporaryDirectory(prefix="mk-trust-", dir="/tmp") as folder:
            archive = Path(folder) / "archive"
            self.store.backup(archive)
            self.store.close()
            credential = LocalStore.restore(self.data, self.control, archive).read_text()
            self.store = LocalStore(self.data, self.control)
            self.owner = credential
            restored = Trust(self.store)
            self.assertEqual(restored.level("main", "p", "scraper-z"), "untrusted")
            self.assertTrue(restored.held("main", held["observation"]))
            self.assertFalse(restored.held("main", kept["observation"]))


class PostgresTrustTests(TrustCases, pg.NativeFixture):
    pass
