"""Entity anchors and salience on both backends: exact names found, linked and ranked."""

import json
import unittest

import test_knowledge as knowledge
import test_postgres_parity as pg

from memoryking.adapters.atlas import query as atlas_query
from memoryking.adapters.retrieval import ARCHIVE_AFTER_MS, Retrieval
from memoryking.domain.anchors import extract


class AnchorExtractionTests(unittest.TestCase):
    def test_extracts_paths_symbols_tickets_urls_hosts_and_errors_exactly(self):
        text = (
            "The fix is in payments/retry.py; see PAY-1234 and https://api.example.com/v2/retry "
            "on host db-primary.internal.example.org. RetryDeadline.compute() raised "
            "TimeoutError (ECONNRESET, HTTP 503). Also run scripts/migrate.sh and check "
            "retry_deadline_ms, issue #4521, and Version 3.12.4."
        )
        found = extract(text)
        kinds = {kind: [a for k, a in found if k == kind] for kind in {k for k, _ in found}}
        self.assertEqual(kinds["path"], ["payments/retry.py", "scripts/migrate.sh"])
        self.assertEqual(kinds["url"], ["https://api.example.com/v2/retry"])
        self.assertEqual(kinds["host"], ["api.example.com", "db-primary.internal.example.org"])
        self.assertEqual(kinds["ticket"], ["PAY-1234", "#4521"])
        self.assertEqual(kinds["error"], ["TimeoutError", "ECONNRESET", "HTTP 503"])
        self.assertIn("RetryDeadline.compute", kinds["symbol"])
        self.assertIn("retry_deadline_ms", kinds["symbol"])
        self.assertNotIn(("host", "retrydeadline.compute"), found)
        self.assertEqual(extract("Weather was nice, went cycling along the river."), [])
        self.assertLessEqual(len(extract(" ".join(f"file{i}.py" for i in range(100)))), 32)
        self.assertEqual(extract("x" * 300 + ".py"), [])


class AnchorCases:
    def capture(self, key, text):
        return self.store.capture(
            self.owner,
            self.ns,
            key,
            {
                "version": 1,
                "project": "p",
                "source": "test",
                "source_event": key,
                "text": text,
                "readers": None,
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )["observation"]

    def trace(self, packet, stage):
        output = []
        Retrieval(self.store).execute(
            self.owner,
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": "receipt",
                "args": {"project": "p", "receipt": packet["receipt"]},
            },
        ).emit(output.append)
        return next(t for t in json.loads(output[0])["diagnostics"]["trace"] if t["stage"] == stage)

    def brief(self, query, **extra):
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
                    **extra,
                },
            },
        ).emit(output.append)
        return json.loads(output[0])

    def test_anchor_in_the_query_finds_records_naming_it_and_links_them_in_the_graph(self):
        a = self.capture(
            "a", "Scoped the deadline per request in payments/retry.py after PAY-1234."
        )
        self.capture("b", "Unrelated note about lunch and cycling.")
        c = self.capture("c", "The test for payments/retry.py still flakes on quartz.internal.")
        packet = self.brief("payments/retry.py")
        cited = {g["reference"]: g for g in packet["guidance"]}
        self.assertEqual(set(cited), {a, c})
        self.assertTrue(all(g["selection_reason"] == "anchor_match" for g in cited.values()))
        self.assertEqual(cited[a]["lexical_score_ppm"], 1000000)
        ticket = self.brief("what happened with PAY-1234")
        self.assertEqual([g["reference"] for g in ticket["guidance"]], [a])
        self.assertEqual(
            {
                (r[0], r[1])
                for r in self.store.db.execute("SELECT id,anchor FROM anchors").fetchall()
            },
            {
                (a, "payments/retry.py"),
                (a, "PAY-1234"),
                (c, "payments/retry.py"),
                (c, "quartz.internal"),
            },
        )
        with self.store.transaction() as decision:
            who = self.store._auth(self.owner, "read", "p", decision.evaluated_ms)
            raw, _, _ = atlas_query(
                Retrieval(self.store), who, {"project": "p", "bindings": [{}]}, decision, "rcpt_t"
            )
        page = json.loads(raw)
        links = [e for e in page["edges"] if e["type"] == "shares_anchor"]
        self.assertEqual(
            [({e["source"], e["target"]}, e["provenance"]["anchor"]) for e in links],
            [({a, c}, "payments/retry.py")],
        )
        node = next(n for n in page["nodes"] if n["id"] == a)
        self.assertEqual([x["anchor"] for x in node["anchors"]], ["PAY-1234", "payments/retry.py"])
        # Purge removes the anchors with the record; reindex rebuilds them from the text.
        self.store.purge(self.owner, c)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM anchors WHERE id=?", (c,)).fetchone()[0], 0
        )
        self.store.db.execute("DELETE FROM anchors")
        self.store.reindex(self.owner)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM anchors").fetchone()[0], 2)

    def test_salience_rewards_delivered_records_and_archives_never_used_old_ones(self):
        fresh = self.capture("fresh", "Zebra timeout note that was delivered and used.")
        old = self.capture("old", "Zebra timeout note nobody ever asked for.")
        tenant = self.store._decode(self.owner, "credential")["tenant"]
        # Age the second record's capture event past the archive threshold.
        self.store.db.execute(
            "UPDATE events SET at_ms=at_ms-? WHERE tenant=? AND subject=?",
            (ARCHIVE_AFTER_MS + 86_400_000, tenant, old),
        )
        first = self.brief("zebra timeout", salience="on")
        by_ref = {g["reference"]: g for g in first["guidance"]}
        self.assertEqual(by_ref[old]["salience"]["tier"], "archival")
        self.assertNotIn("salience", by_ref[fresh])
        self.assertEqual([g["reference"] for g in first["guidance"]][-1], old)
        self.assertEqual(self.trace(first, "salience_v1")["archival_candidates"], 1)
        # Off unless asked: the next briefing carries no salience and says so.
        default = self.brief("zebra timeout")
        self.assertTrue(all("salience" not in g for g in default["guidance"]))
        self.assertEqual(self.trace(default, "salience_v1")["state"], "off_unless_requested")
        # Both earlier briefings delivered the fresh record; that use now counts.
        second = self.brief("zebra timeout", salience="on")
        by_ref = {g["reference"]: g for g in second["guidance"]}
        self.assertEqual(by_ref[fresh]["salience"]["deliveries_90d"], 2)
        self.assertGreater(by_ref[fresh]["salience"]["strength_ppm"], 0)
        self.assertEqual(by_ref[fresh]["salience"]["tier"], "active")
        historical = self.brief(
            "zebra timeout", salience="on", recorded_position=second["snapshot"]["source_position"]
        )
        self.assertTrue(all("salience" not in g for g in historical["guidance"]))
        self.assertEqual(
            self.trace(historical, "salience_v1")["state"], "disabled_for_historical_view"
        )


class LocalAnchorTests(AnchorCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown


class PostgresAnchorTests(AnchorCases, pg.NativeFixture):
    pass
