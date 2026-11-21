"""Word forms, term rarity, candidate selection and the lexical index lifecycle, on both backends.

Every case runs on SQLite and on native PostgreSQL; the parity case asserts one literal
ordered result for a mixed corpus, so the two engines must agree exactly.
"""

import json
import unittest

import test_knowledge as knowledge
import test_postgres_parity as pg

from memoryking.adapters.retrieval import Retrieval
from memoryking.application import lexical_index
from memoryking.domain.briefing import CONFIGURATION, LEGACY_CONFIGURATION
from memoryking.domain.lexicon import index_terms


class LexicalCases:
    clock_ms = 0

    def capture(self, key, text, readers=None, observed=None):
        self.clock_ms += 1
        return self.store.capture(
            self.owner,
            self.ns,
            key,
            {
                "version": 1,
                "project": "q",
                "source": "test",
                "source_event": key,
                "text": text,
                "readers": readers,
                "classification": "internal",
                "observed_at_ms": observed or self.clock_ms,
            },
        )["observation"]

    def brief(self, query, credential=None, **extra):
        output = []
        Retrieval(self.store).execute(
            credential or self.owner,
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": "brief",
                "args": {
                    "project": "q",
                    "query": query,
                    "bindings": [{}],
                    "max_output_tokens": 6000,
                    "tokenizer": "utf8-byte-estimate-v1",
                    **extra,
                },
            },
        ).emit(output.append)
        return json.loads(output[0])

    def receipt(self, packet):
        output = []
        Retrieval(self.store).execute(
            self.owner,
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": "receipt",
                "args": {"project": "q", "receipt": packet["receipt"]},
            },
        ).emit(output.append)
        return json.loads(output[0])["diagnostics"]

    def refs(self, packet):
        return [g["reference"] for g in packet["guidance"]]

    def tenant_id(self):
        return self.store.db.execute("SELECT tenant FROM tenants").fetchone()[0]

    def statistics(self):
        tenant = self.tenant_id()
        terms = {
            (r[0], r[1]): r[2]
            for r in self.store.db.execute(
                "SELECT project,term,documents FROM lexical_terms WHERE tenant=?", (tenant,)
            ).fetchall()
        }
        projects = {
            r[0]: (r[1], r[2])
            for r in self.store.db.execute(
                "SELECT project,documents,tokens FROM lexical_projects WHERE tenant=?", (tenant,)
            ).fetchall()
        }
        return terms, projects

    def recomputed(self):
        """Statistics rebuilt from scratch out of the stored index text of live observations."""
        tenant = self.tenant_id()
        terms: dict = {}
        projects: dict = {}
        for row in self.store.db.execute(
            "SELECT o.project,l.text FROM lexical l JOIN observations o ON o.tenant=l.tenant AND o.id=l.id WHERE l.tenant=? AND o.deleted=0",
            (tenant,),
        ).fetchall():
            found, length = index_terms(row[1])
            count, tokens = projects.get(row[0], (0, 0))
            projects[row[0]] = (count + 1, tokens + length)
            for term in found:
                terms[(row[0], term)] = terms.get((row[0], term), 0) + 1
        return terms, projects

    def lexical_trace(self, packet):
        return next(t for t in self.receipt(packet)["trace"] if t["stage"] == "lexical")

    # --- the five measured failures -----------------------------------------------------

    def test_word_forms_meet(self):
        target = self.capture("a", "The retries double-charged several customers during checkout.")
        self.capture("b", "Quarterly planning notes about hiring.")
        for query in ("retry charge customer", "retrying charging customer", "customers retried"):
            packet = self.brief(query)
            self.assertIn(target, self.refs(packet), query)
            self.assertEqual("partial", packet["status"])
        self.assertEqual(CONFIGURATION, self.receipt(self.brief("retry"))["configuration"])

    def test_exact_words_score_at_least_as_high_as_other_forms(self):
        exact = self.capture("exact", "retry the charge")
        other = self.capture("other", "retries charged")
        packet = self.brief("retry charge")
        scores = {g["reference"]: g["lexical_score_ppm"] for g in packet["guidance"]}
        self.assertGreater(scores[exact], scores[other])
        self.assertEqual(exact, self.refs(packet)[0])

    def test_a_rare_term_outranks_common_ones(self):
        for number in range(12):
            self.capture(
                f"c{number}", f"Routine deploy config change number {number} was reviewed."
            )
        rare = self.capture("rare", "Root cause was the zukatori flag being unset in production.")
        packet = self.brief("deploy config zukatori", top_k=8)
        self.assertEqual(rare, self.refs(packet)[0])

    def test_the_only_note_with_every_term_is_found_in_a_crowded_project(self):
        target = self.capture(
            "all", "Decision: the ledger reconciliation webhook must be idempotent."
        )
        for term in ("ledger", "reconciliation", "webhook"):
            for number in range(258):
                self.capture(
                    f"{term}{number}", f"Routine note {number} mentioning {term} in passing."
                )
        packet = self.brief("ledger reconciliation webhook")
        self.assertEqual(target, self.refs(packet)[0])
        self.assertGreater(
            packet["guidance"][0]["lexical_score_ppm"], packet["guidance"][1]["lexical_score_ppm"]
        )

    def test_equal_scores_order_newest_first_so_a_correction_beats_the_stale_fact(self):
        stale = self.capture("old", "The gateway timeout is thirty seconds.", observed=10)
        fresh = self.capture("new", "The gateway timeout is sixty seconds.", observed=20)
        for _ in range(3):
            packet = self.brief("gateway timeout")
            self.assertEqual([fresh, stale], self.refs(packet))
            scores = {g["lexical_score_ppm"] for g in packet["guidance"]}
            self.assertEqual(1, len(scores))

    # --- statistics stay exact ------------------------------------------------------------

    def test_statistics_follow_capture_and_purge_and_a_rebuild_reproduces_them(self):
        first = self.capture("a", "The retries double-charged customers.")
        self.capture("b", "Payments retry_charge failed with PAY-142 on src/app/main.py")
        third = self.capture("c", "retry retry retry")
        self.assertEqual(self.recomputed(), self.statistics())
        self.assertEqual(3, self.statistics()[1]["q"][0])
        self.store.purge(self.owner, first)
        self.assertEqual(self.recomputed(), self.statistics())
        self.store.purge(self.owner, third)
        self.assertEqual(self.recomputed(), self.statistics())
        self.assertEqual(1, self.statistics()[1]["q"][0])
        before = self.statistics()
        self.store.reindex(self.owner)
        self.assertEqual(before, self.statistics())
        self.assertEqual("current", self.store.status(self.owner, "q")["lexical"]["state"])

    def test_a_reader_never_sees_what_the_statistics_could_reveal(self):
        self.capture("a", "zukatori secret launch plan", readers=["someone_else"])
        self.capture("b", "ordinary launch notes")
        packet = self.brief("zukatori launch")
        self.assertEqual(1, len(packet["guidance"]))
        text = json.dumps([packet, self.receipt(packet)])
        for word in ("frequency", "documents", "df", "idf"):
            self.assertNotIn(f'"{word}"', text)
        trace = self.lexical_trace(packet)
        self.assertEqual("whole_project_current_corpus", trace["statistics"])
        self.assertEqual(
            {
                "stage",
                "visible_candidates",
                "candidate_limit_reached",
                "max_candidates",
                "configuration",
                "selection",
                "searches",
                "statistics",
            },
            set(trace),
        )
        # held back text never ranks above readable text just because of its rarity
        self.assertNotIn("secret", json.dumps(packet))

    def test_historical_views_use_current_corpus_statistics_and_say_so(self):
        first = self.capture("a", "retry budget exhausted")
        position = self.store.db.execute(
            "SELECT position FROM observations WHERE id=?", (first,)
        ).fetchone()[0]
        self.capture("b", "retry budget raised")
        packet = self.brief("retry budget", recorded_position=position)
        self.assertEqual([first], self.refs(packet))
        self.assertEqual("whole_project_current_corpus", self.lexical_trace(packet)["statistics"])

    # --- the index lifecycle ----------------------------------------------------------------

    def make_legacy(self):
        """Put the store in the shape an upgrade from schema 15 leaves it in."""
        tenant = self.tenant_id()
        rows = self.store.db.execute(
            "SELECT o.id,o.blob FROM observations o WHERE o.tenant=? AND o.deleted=0", (tenant,)
        ).fetchall()
        for row in rows:
            raw = lexical_index.blob_text(self.store.root, row, tenant)
            self.store.db.execute(
                "UPDATE lexical SET text=? WHERE tenant=? AND id=?", (raw, tenant, row[0])
            )
        self.store.db.execute("DELETE FROM lexical_terms WHERE tenant=?", (tenant,))
        self.store.db.execute("DELETE FROM lexical_projects WHERE tenant=?", (tenant,))
        self.store.db.execute("DELETE FROM lexical_state WHERE tenant=?", (tenant,))

    def test_an_unrebuilt_store_keeps_working_and_reindex_moves_it_to_the_new_ranking(self):
        old = self.capture("a", "The retries double-charged several customers during checkout.")
        self.capture("b", "Gateway timeout is thirty seconds.")
        self.make_legacy()
        status = self.store.status(self.owner, "q")["lexical"]
        self.assertEqual("legacy", status["state"])
        self.assertEqual(LEGACY_CONFIGURATION, status["configuration"])
        live = self.store.db.execute(
            "SELECT count(*) FROM observations WHERE deleted=0"
        ).fetchone()[0]
        self.assertEqual(live, status["remaining_observations"])
        self.assertIn("reindex", status["action"])
        # old rows are found by their exact token, and the trace names the old ranking
        packet = self.brief("retries")
        self.assertEqual([old], self.refs(packet))
        self.assertEqual(LEGACY_CONFIGURATION, self.receipt(packet)["configuration"])
        # a capture while legacy still works and is found
        fresh = self.capture("c", "Gateway timeout raised to sixty seconds.")
        self.assertIn(fresh, self.refs(self.brief("gateway timeout")))
        self.assertEqual("no_relevant_memory", self.brief("retry charge customer")["status"])
        self.store.reindex(self.owner)
        status = self.store.status(self.owner, "q")["lexical"]
        self.assertEqual("current", status["state"])
        self.assertEqual(self.recomputed(), self.statistics())
        packet = self.brief("retry charge customer")
        self.assertEqual([old], self.refs(packet))
        self.assertEqual(CONFIGURATION, self.receipt(packet)["configuration"])

    def test_the_rebuild_runs_in_batches_resumes_and_does_not_block_captures(self):
        keys = [self.capture(f"d{n}", f"Note {n} about retries and charges.") for n in range(7)]
        tenant = self.tenant_id()
        original = lexical_index.BATCH_DOCUMENTS
        lexical_index.BATCH_DOCUMENTS = 2
        try:
            with self.store.transaction():
                lexical_index.begin(self.store.db, tenant)
            steps = 0
            while True:
                done = self.store.rebuild_lexical(wall_ms=0)
                state = self.store.status(self.owner, "q")["lexical"]
                if done:
                    break
                steps += 1
                if state["state"] == "rebuilding":
                    self.assertEqual("lexical-v1", state["configuration"])
                if steps == 2:
                    # a capture and a purge in the middle of a rebuild
                    mid = self.capture("mid", "A late note about retries.")
                    self.store.purge(self.owner, keys[0])
                self.assertLess(steps, 40)
            self.assertGreaterEqual(steps, 4)
        finally:
            lexical_index.BATCH_DOCUMENTS = original
        self.assertEqual("current", self.store.status(self.owner, "q")["lexical"]["state"])
        self.assertEqual(self.recomputed(), self.statistics())
        self.assertIn(mid, self.refs(self.brief("retry")))
        self.assertNotIn(keys[0], self.refs(self.brief("retry")))

    def test_a_new_store_starts_current(self):
        self.assertEqual("current", self.store.status(self.owner, "q")["lexical"]["state"])

    # --- both engines return one ordered result ----------------------------------------------

    def test_a_mixed_corpus_returns_the_same_ordered_list_on_both_engines(self):
        docs = {
            "main": "Entry point is src/app/main.py and it calls payments.retry_charge() on boot.",
            "ticket": "PAY-142 tracks the double charge when retry_charge() runs twice.",
            "prose": "The retries double-charged several customers during checkout.",
            "path": "Moved helpers out of src/app/utils.py into src/lib/helpers.py",
            "noise": "Quarterly planning notes about hiring and onboarding.",
            "error": "TimeoutError after 30 seconds, see HTTP 503 from api.example.com",
        }
        labels = {self.capture(key, text): key for key, text in docs.items()}
        # One literal result for both engines: labels with their scores in millionths.
        expected = {
            "src/app/main.py": [("main", 1000000), ("path", 356662)],
            "payments.retry_charge()": [("main", 1000000), ("ticket", 300728)],
            "PAY-142": [("ticket", 1000000)],
            "retry charge customer": [("prose", 457665), ("ticket", 318054), ("main", 251303)],
            "src app": [("path", 505942), ("main", 401929)],
            "timeouts api example": [("error", 237069)],
            "HTTP 503": [("error", 1000000)],
            "double charges": [("prose", 485506), ("ticket", 413120)],
        }
        for query, result in expected.items():
            packet = self.brief(query)
            got = [(labels[g["reference"]], g["lexical_score_ppm"]) for g in packet["guidance"]]
            self.assertEqual(result, got, query)


class LocalLexicalTests(LexicalCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown


class LocalMigrationTests(unittest.TestCase):
    clock_ms = 0
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown

    def test_a_schema_15_store_migrates_keeps_its_rows_searchable_and_is_reported_by_doctor(self):
        from memoryking.__main__ import lexical_findings
        from memoryking.adapters.local_store import SCHEMA_VERSION, LocalStore

        LexicalCases.capture(self, "old", "retries of the nightly export job")
        LexicalCases.capture(self, "other", "unrelated hiring plan")
        tenant = self.store.db.execute("SELECT tenant FROM tenants").fetchone()[0]
        for table in ("lexical_terms", "lexical_projects", "lexical_state"):
            self.store.db.execute(f"DROP TABLE {table}")
        self.store.db.execute("UPDATE lexical SET text='retries of the nightly export job'")
        self.store.db.execute("PRAGMA user_version=15")
        self.store.close()
        self.store = LocalStore(self.data, self.control)
        self.assertEqual(17, SCHEMA_VERSION)
        self.assertEqual(17, self.store.db.execute("PRAGMA user_version").fetchone()[0])
        status = self.store.status(self.owner, "q")["lexical"]
        self.assertEqual("legacy", status["state"])
        self.assertEqual("lexical-v1", status["configuration"])
        packet = LexicalCases.brief(self, "retries")
        self.assertEqual(1, len(packet["guidance"]))
        self.store.close()
        import sqlite3

        with sqlite3.connect(f"file:{self.data / 'store.sqlite'}?mode=ro", uri=True) as db:
            found = lexical_findings(db, 16)
        self.assertEqual([(tenant, "legacy", 3)], found)
        self.store = LocalStore(self.data, self.control)
        self.store.reindex(self.owner)
        self.assertEqual("current", self.store.status(self.owner, "q")["lexical"]["state"])


class PostgresLexicalTests(LexicalCases, pg.NativeFixture):
    pass


if __name__ == "__main__":
    unittest.main()
