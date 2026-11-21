"""Retention for tables that only grow, and the receipt-to-observation join, on both backends.

Rows are inserted directly where a case needs thousands of real writes; the real write paths
are exercised by the suites around them.
"""

import json
import unittest
from unittest import mock

import test_knowledge as knowledge
import test_postgres_parity as pg
import test_working as working
from test_experience_records import RecordCases
from test_working import checkpoint

from memoryking.adapters.execution import Execution
from memoryking.application import retention
from memoryking.domain.local import LocalError

DAY = 24 * 60 * 60 * 1000


class Skewed:
    """The system clock plus an offset the test advances."""

    def __init__(self):
        self.offset = 0

    def utc_ms(self):
        import time

        return time.time_ns() // 1_000_000 + self.offset

    def monotonic_ms(self):
        import time

        return time.monotonic_ns() // 1_000_000

    def advance(self, ms):
        self.offset += ms


class RetentionCases:
    run_e = RecordCases.run_e
    run_r = RecordCases.run_r
    brief_args = RecordCases.brief_args
    feedback = RecordCases.feedback
    records = RecordCases.records
    scope = working.WorkingTests.scope
    command = working.WorkingTests.command
    execute = working.WorkingTests.execute
    save = working.WorkingTests.save
    fabricate_checkpoints = working.WorkingTests.fabricate_checkpoints
    tenant_id = working.WorkingTests.tenant_id

    def skew(self):
        self.store.clock = Skewed()
        return self.store.clock

    def sql(self, statement, params=()):
        with self.store.transaction():
            cursor = self.store.db.execute(statement, params)
            return cursor.fetchall() if statement.lstrip().upper().startswith("SELECT") else []

    def count(self, table, where="1=1", params=()):
        return self.sql(
            f"SELECT count(*) FROM {table} WHERE tenant=? AND {where}", (self.tenant, *params)
        )[0][0]

    def insert_artifact(self, ident, state, created, expires, dependencies="[]"):
        body = b"x" if state == "retained" else None
        diagnostics = "{}" if state == "retained" else None
        self.sql(
            "INSERT INTO rendered_artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.tenant,
                ident,
                "owner",
                "p",
                self.store.incarnation,
                created,
                expires,
                state,
                body,
                "h" * 64,
                None,
                diagnostics,
                dependencies,
            ),
        )

    def capture_more(self, count):
        ns = self.store.namespace(self.owner, "p")
        for number in range(count):
            self.store.capture(
                self.owner,
                ns,
                f"more{number}",
                {
                    "version": 1,
                    "project": "p",
                    "source": "test",
                    "source_event": f"more{number}",
                    "text": f"another durable memory {number}",
                    "readers": None,
                    "classification": "internal",
                    "observed_at_ms": 1,
                },
            )

    def test_purge_deletes_exactly_the_receipts_that_list_the_observation(self):
        brief = self.run_r()
        self.assertTrue(brief["receipt"])
        target = self.obs["observation"]
        self.assertTrue(self.count("receipt_observations", "observation=?", (target,)))
        # A receipt that lists something else is untouched, and a purge of a different
        # observation does not remove the first receipt.
        self.sql(
            "INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                self.tenant,
                "rcpt_other",
                "owner",
                self.store.incarnation,
                1,
                1,
                0,
                0,
                "h" * 64,
                json.dumps(["unrelated"]),
            ),
        )
        with self.store.transaction():
            retention.index_receipt(self.store.db, self.tenant, "rcpt_other")
        self.store.purge(self.owner, target)
        self.assertEqual(
            0, self.count("receipts", "observation_ids LIKE ?", ('%"' + target + '"%',))
        )
        self.assertEqual(0, self.count("receipt_observations", "observation=?", (target,)))
        self.assertEqual(1, self.count("receipts", "id='rcpt_other'"))
        self.assertEqual(1, self.count("receipt_observations", "receipt='rcpt_other'"))

    def test_finished_jobs_behind_the_frontier_are_deleted_and_a_reindex_creates_them_again(self):
        self.capture_more(5)
        with mock.patch.object(retention, "JOB_KEEP", 1):
            while self.store.drain():
                pass
            total = self.count("events")
            self.assertEqual(total, self.store.status(self.owner, "p")["projection"]["frontier"])
            # Only the newest job stays (the keep count is 1); every older one is gone.
            self.assertEqual(1, self.count("jobs"))
            self.assertEqual({"complete": 1}, self.store.status(self.owner, "p")["jobs"])
            self.store.reindex(self.owner)
            # The rebuild needs a job per event position and creates the missing ones.
            self.assertEqual(total, self.count("jobs"))
            self.assertEqual(0, self.store.status(self.owner, "p")["projection"]["frontier"])
            while self.store.drain():
                pass
            self.assertEqual(total, self.store.status(self.owner, "p")["projection"]["frontier"])
        self.assertEqual(6, self.count("projection_items"))
        target = self.obs["observation"]
        self.store.purge(self.owner, target)
        self.assertEqual(0, self.count("projection_items", "observation=?", (target,)))

    def test_artifact_tombstones_go_thirty_days_after_they_expired_and_live_ones_stay(self):
        clock = self.skew()
        now = clock.utc_ms()
        old = now - 40 * DAY
        for name, state in (
            ("a_exp", "expired"),
            ("a_evi", "evicted"),
            ("a_pur", "purged"),
            ("a_ret", "retired"),
        ):
            self.insert_artifact(name, state, old - 30 * DAY, old)
        self.insert_artifact("a_recent", "expired", now - 20 * DAY, now - 5 * DAY)
        self.insert_artifact("a_live", "retained", now - DAY, now + 29 * DAY)
        self.insert_artifact("a_due", "retained", now - 31 * DAY, now - DAY)
        self.store.expire_results()
        left = {
            r[0]: r[1]
            for r in self.sql(
                "SELECT id,state FROM rendered_artifacts WHERE tenant=?", (self.tenant,)
            )
        }
        self.assertEqual({"a_recent": "expired", "a_live": "retained", "a_due": "expired"}, left)
        # Thirty days later the one that just expired is a tombstone to delete as well.
        clock.advance(31 * DAY)
        self.store.expire_results()
        self.assertEqual(
            ["a_live"],
            [
                r[0]
                for r in self.sql(
                    "SELECT id FROM rendered_artifacts WHERE tenant=?", (self.tenant,)
                )
            ],
        )

    def test_feedback_records_expire_after_a_year_unless_something_names_them(self):
        clock = self.skew()
        self.feedback()
        outcome = self.outcome_record()
        feedback_id = self.sql(
            "SELECT id FROM experience_records WHERE tenant=? AND kind='feedback'", (self.tenant,)
        )[0][0]
        self.store.expire_results()
        self.assertEqual(
            1, self.count("experience_records", "kind='feedback' AND body IS NOT NULL")
        )
        clock.advance(366 * DAY)
        # A retained artifact that cites the record keeps it.
        self.insert_artifact(
            "a_cites",
            "retained",
            clock.utc_ms(),
            clock.utc_ms() + 3650 * DAY,
            json.dumps([{"kind": "experience_record", "id": feedback_id}]),
        )
        self.store.expire_results()
        self.assertEqual(
            1, self.count("experience_records", "kind='feedback' AND body IS NOT NULL")
        )
        self.sql("DELETE FROM rendered_artifacts WHERE tenant=? AND id='a_cites'", (self.tenant,))
        self.store.expire_results()
        self.assertEqual(
            0, self.count("experience_records", "kind='feedback' AND body IS NOT NULL")
        )
        # The row stays as the consumed identity; other kinds never expire by age.
        self.assertEqual(1, self.count("experience_records", "kind='feedback'"))
        self.assertEqual(1, self.count("experience_records", "kind='outcome' AND body IS NOT NULL"))
        self.assertIsNotNone(outcome)
        with self.assertRaises(LocalError) as raised:
            self.feedback()
        self.assertEqual("experience_revision_conflict", raised.exception.code)

    def outcome_record(self):
        return RecordCases.outcome(self)

    def test_above_eighty_percent_ordinary_streams_keep_32_revisions_and_plans_keep_all(self):
        self.fabricate_checkpoints(8, 400)  # 3,200 live: below 80 percent of 4,096
        self.store.expire_results()
        self.assertEqual(8 * 130, self.count("checkpoints", "state='available'"))
        self.fabricate_checkpoints(20, 130, prefix="more")  # 3,640 live: above 80 percent
        plan = json.dumps({"plan": {"steps": []}})
        self.sql(
            "UPDATE checkpoints SET body=? WHERE tenant=? AND id='ckpt_bulk0_400'",
            (plan, self.tenant),
        )
        self.store.expire_results()
        by_task = {
            r[0]: r[1]
            for r in self.sql(
                "SELECT task,count(*) FROM checkpoints WHERE tenant=? AND state='available' GROUP BY task",
                (self.tenant,),
            )
        }
        self.assertEqual(130, by_task["bulk0"])  # a plan stream keeps everything it had
        self.assertEqual({32}, {by_task[f"bulk{n}"] for n in range(1, 8)})
        self.assertEqual({32}, {by_task[f"more{n}"] for n in range(20)})

    def test_a_stream_that_released_revisions_reports_incomplete_history(self):
        self.save()
        self.save(checkpoint(), 1)
        self.save(checkpoint(), 2)
        self.assertTrue(self.view()["tasks"][0]["progress"]["history_complete"])
        self.sql(
            "UPDATE checkpoints SET state='retired',body=NULL,dependencies='[]' WHERE tenant=? AND revision=1",
            (self.tenant,),
        )
        self.assertFalse(self.view()["tasks"][0]["progress"]["history_complete"])


class LocalRetentionTests(RetentionCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown

    def view(self):
        with self.store.transaction() as decision:
            who = self.store._auth(self.owner, "read", "p", decision.evaluated_ms)
            return Execution(self.store).snapshot(who, "p", decision.evaluated_ms)

    def plan(self, statement, params):
        return " | ".join(
            r[3] for r in self.store.db.execute("EXPLAIN QUERY PLAN " + statement, params)
        )

    def test_the_scans_that_grew_with_the_tables_now_use_indexes(self):
        t = (self.tenant,)
        plans = {
            "expire": self.plan(
                "UPDATE rendered_artifacts SET state='expired' WHERE tenant=? AND state='retained' AND expires<=?",
                (*t, 1),
            ),
            "room": self.plan(
                "SELECT id FROM rendered_artifacts WHERE tenant=? AND principal=? AND state='retained' ORDER BY created,id LIMIT 1",
                (*t, "owner"),
            ),
            "as_of": self.plan(
                "SELECT position FROM events WHERE tenant=? AND at_ms<=? ORDER BY at_ms DESC,position DESC LIMIT 1",
                (*t, 1),
            ),
            "harness": self.plan(
                "SELECT o.* FROM observations o WHERE o.tenant=? AND o.project=? AND o.source=? AND o.position<=? ORDER BY o.position DESC LIMIT 128",
                (*t, "p", "s", 1),
            ),
            "purge": self.plan(
                "DELETE FROM receipts WHERE tenant=? AND id IN (SELECT receipt FROM receipt_observations WHERE tenant=? AND observation=?)",
                (*t, *t, "o"),
            ),
            "retries": self.plan(
                "UPDATE retries SET result=NULL WHERE tenant=? AND result IS NOT NULL AND expires<=?",
                (*t, 1),
            ),
            "spans": self.plan(
                "SELECT revision FROM evidence_spans WHERE tenant=? AND observation=?", (*t, "o")
            ),
        }
        for name, plan in plans.items():
            self.assertNotRegex(
                plan,
                r"SCAN (rendered_artifacts|events|observations|receipts|retries|evidence_spans)\b(?! USING)",
                name,
            )
            self.assertIn("USING", plan, name)

    def test_a_schema_18_store_migrates_and_fills_the_receipt_join(self):
        from memoryking.adapters.local_store import SCHEMA_VERSION, LocalStore

        self.run_r()
        before = self.count("receipt_observations")
        self.assertGreater(before, 0)
        self.store.db.execute("DROP TABLE receipt_observations")
        for index in (
            "rendered_retained",
            "events_time",
            "observations_source",
            "evidence_spans_observation",
        ):
            self.store.db.execute(f"DROP INDEX {index}")
        self.store.db.execute("PRAGMA user_version=18")
        self.store.close()
        self.store = LocalStore(self.data, self.control)
        self.assertEqual(19, SCHEMA_VERSION)
        self.assertEqual(19, self.store.db.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual(before, self.count("receipt_observations"))
        names = {
            r[0] for r in self.store.db.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        self.assertTrue(
            {
                "rendered_retained",
                "events_time",
                "observations_source",
                "evidence_spans_observation",
            }
            <= names
        )
        self.store.purge(self.owner, self.obs["observation"])
        self.assertEqual(0, self.count("receipts", "observation_ids<>'[]'"))


class PostgresRetentionTests(RetentionCases, pg.NativeFixture):
    def view(self):
        with self.store.transaction() as decision:
            who = self.store._auth(self.owner, "read", "p", decision.evaluated_ms)
            return Execution(self.store).snapshot(who, "p", decision.evaluated_ms)

    def test_a_schema_13_database_migrates_and_fills_the_receipt_join(self):
        import psycopg

        from memoryking.adapters.postgres_admin import migrate

        self.run_r()
        before = self.count("receipt_observations")
        self.assertGreater(before, 0)
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as db:
            with db.transaction():
                db.execute("SET LOCAL search_path=memoryking,pg_catalog")
                db.execute("DROP TABLE receipt_observations")
                db.execute("UPDATE shared_meta SET value='13' WHERE key='schema'")
            migrate(db)
            version = db.execute(
                "SELECT value FROM memoryking.shared_meta WHERE key='schema'"
            ).fetchone()[0]
        self.assertEqual("14", version)
        self.assertEqual(before, self.count("receipt_observations"))
        # Row-level security is forced again after the copy.
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as db:
            forced = db.execute(
                "SELECT relforcerowsecurity FROM pg_class WHERE oid='memoryking.receipts'::regclass"
            ).fetchone()[0]
        self.assertTrue(forced)


if __name__ == "__main__":
    unittest.main()
