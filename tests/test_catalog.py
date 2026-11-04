"""The catalog inspection on both backends: records as a paged listing, visibility kept."""

import json
import unittest

import test_knowledge as knowledge
import test_postgres_parity as pg

from memoryking.adapters.inspection import Inspection


class CatalogCases:
    body = knowledge.KnowledgeTests.body
    propose = knowledge.KnowledgeTests.propose
    review = knowledge.KnowledgeTests.review
    run_k = knowledge.KnowledgeTests.run_k

    def capture(self, key, text, source="notes", credential=None):
        return self.store.capture(
            credential or self.owner,
            self.ns if credential is None else self.store.namespace(credential, "p"),
            key,
            {
                "version": 1,
                "project": "p",
                "source": source,
                "source_event": key,
                "text": text,
                "readers": None,
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )["observation"]

    def as_principal(self, credential):
        """The store seen by another credential: PostgreSQL binds one principal per store."""
        if not hasattr(self, "pool"):
            return self.store
        from memoryking.adapters.postgres_store import PostgresStore

        store = PostgresStore(self.pool, self.data, self.control, self.tenant, credential)
        store.db = pg.InspectionSQL(store, store.db)
        self.addCleanup(store.close)
        return store

    def catalog(self, credential=None, **args):
        output = []
        Inspection(self.store if credential is None else self.as_principal(credential)).execute(
            credential or self.owner,
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": "catalog",
                "args": {"project": "p", **args},
            },
        ).emit(output.append)
        return json.loads(output[0])

    def test_observations_newest_first_with_filters_pages_and_versions(self):
        a = self.capture("plan", "First plan.")
        b = self.capture("plan__v2", "Second plan.")
        self.capture("planet", "Not a version of plan.")
        c = self.capture("a_b", "Underscore name.")
        self.capture("axb", "Looks alike under LIKE.")
        page = self.catalog()
        self.assertEqual(page["kind"], "observations")
        self.assertEqual(page["total"], 6)  # the fixture's own observation counts
        self.assertEqual(page["items"][1]["id"], c)
        self.assertGreater(page["items"][0]["position"], page["items"][1]["position"])
        self.assertEqual(page["items"][0]["text"], "Looks alike under LIKE.")
        self.assertEqual(
            sorted(page["items"][0]),
            sorted(
                [
                    "id",
                    "source",
                    "source_event",
                    "principal",
                    "position",
                    "recorded_ms",
                    "observed_at_ms",
                    "classification",
                    "readers",
                    "bytes",
                    "hash",
                    "text",
                ]
            ),
        )
        # A name lists exactly its versions; `_` is literal, not a wildcard.
        versions = self.catalog(source="notes", name="plan")
        self.assertEqual([i["id"] for i in versions["items"]], [b, a])
        self.assertEqual([i["id"] for i in self.catalog(name="a_b")["items"]], [c])
        self.assertEqual(self.catalog(source="test")["total"], 1)
        # Pages walk down by position; text can be left out.
        first = self.catalog(limit=2, text=False)
        self.assertEqual(len(first["items"]), 2)
        self.assertNotIn("text", first["items"][0])
        second = self.catalog(limit=2, after=first["next_after"])
        self.assertLess(second["items"][0]["position"], first["items"][-1]["position"])
        self.assertEqual(self.catalog(id=c)["items"][0]["text"], "Underscore name.")

    def test_claims_show_accepted_to_readers_and_every_state_to_reviewers(self):
        proposed = self.propose(self.body("Timeouts are thirty seconds."), key="timeouts")
        reviewed = self.review({"revision": proposed["revision"], "family_version": 1})
        other = self.propose(self.body("Retries are three."), key="retries")
        reviewer = self.catalog(kind="claims")
        self.assertEqual(reviewer["kind"], "claims")
        self.assertTrue(reviewer["reviewer"])
        by_key = {item["logical_key"]: item for item in reviewer["items"]}
        self.assertEqual(by_key["timeouts"]["state"], "accepted")
        self.assertEqual(by_key["retries"]["state"], "proposed")
        self.assertEqual(by_key["retries"]["revision"], other["revision"])
        self.assertEqual(by_key["timeouts"]["evidence"], [self.obs["observation"]])
        self.assertEqual(by_key["timeouts"]["text"], "Timeouts are thirty seconds.")
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        seen = self.catalog(credential=reader, kind="claims")
        self.assertFalse(seen["reviewer"])
        self.assertEqual([i["logical_key"] for i in seen["items"]], ["timeouts"])
        # A superseding proposal replaces the family's file; the name filter finds it.
        again = self.propose(
            self.body("Timeouts are sixty seconds."),
            key="timeouts",
            version=reviewed["family_version"],
        )
        latest = self.catalog(kind="claims", name="timeouts")["items"]
        self.assertEqual(
            [(i["revision"], i["state"]) for i in latest], [(again["revision"], "proposed")]
        )
        self.assertEqual(self.catalog(kind="claims", name="timeouts")["total"], 1)


class LocalCatalogTests(CatalogCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown


class PostgresCatalogTests(CatalogCases, pg.NativeFixture):
    pass
