"""A briefing does not serialize a tenant, and a read is not a durable write.

Briefings share the tenant gate with each other and with captures; policy changes, purges and
restores still hold it alone. Model inference runs with no gate or lock held and the briefing
then decides afresh. A request that moved no history writes no anchor, a change of history is
anchored before it is acknowledged, and a rollback behind either is still refused."""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import test_knowledge as knowledge
import test_postgres_parity as pg
import test_reranker as rr
from test_local_store import FakeClock, observation

from memoryking.adapters.local_files import Control
from memoryking.adapters.local_store import ANCHOR_INTERVAL_MS, LocalStore
from memoryking.adapters.retrieval import Retrieval
from memoryking.adapters.shared_control import SharedControl
from memoryking.adapters.shared_service import SharedService
from memoryking.domain.local import LocalError, Released


class Counting:
    """Records every statement a store issues, passing everything else through."""

    def __init__(self, delegate):
        self.delegate, self.log = delegate, []

    def execute(self, query, args=()):
        self.log.append(query)
        return self.delegate.execute(query, args)

    def __getattr__(self, name):
        return getattr(self.delegate, name)


class SlowReranker(rr.FakeReranker):
    """Blocks inside the model until released, so a test can act while inference runs."""

    def __init__(self):
        super().__init__("alpha")
        self.entered, self.release = threading.Event(), threading.Event()

    def score(self, query, texts):
        self.entered.set()
        if not self.release.wait(30):
            raise AssertionError("the model was never released")
        return super().score(query, texts)


class BriefingCases:
    """Run on both backends: what a briefing may and may not hold or ask while it works."""

    run_r = rr.RerankCases.run_r
    brief_args = rr.RerankCases.brief_args
    capture = rr.RerankCases.capture
    stage = rr.RerankCases.stage

    def position(self):
        return self.store.status(self.owner, "p")["position"]

    def background_brief(self, **kw):
        box = {}

        def run():
            try:
                box["result"] = self.run_r(**kw)
            except BaseException as exc:  # reported by the test thread
                box["error"] = exc

        thread = threading.Thread(target=run)
        thread.start()
        return thread, box

    def test_model_inference_holds_no_gate_and_the_decision_is_taken_afterwards(self):
        self.capture("alpha", "timeout alpha: the first note about the timeout.")
        self.capture("beta", "timeout beta: a later note about the timeout.")
        slow = self.store.reranker = SlowReranker()
        thread, box = self.background_brief(args=self.brief_args(rerank="on"))
        try:
            self.assertTrue(slow.entered.wait(30))
            # With the model running, other work is not queued behind it.
            started = time.monotonic()
            self.capture("late", "timeout alpha: a note that arrived during inference.")
            self.run_r()
            self.assertLess(time.monotonic() - started, 10)
            late_position = self.position()
        finally:
            slow.release.set()
            thread.join(60)
        self.assertNotIn("error", box, box.get("error"))
        result = box["result"]
        stage, diagnostics = self.stage(result)
        self.assertEqual("applied", stage["state"])
        # The released briefing is a fresh decision made after the model finished.
        self.assertGreaterEqual(diagnostics["source_position"], late_position)
        # The window changed while the model ran (the late note joined it), so the fresh
        # decision asked the model again instead of reusing scores for a different window.
        self.assertEqual(2, len(slow.calls))
        self.assertIn("late", " ".join(slow.calls[-1][1]))

    def reader_store(self, credential):
        """The store a reader's request runs on (a shared store is bound to one credential)."""
        return self.store

    def test_a_revocation_during_inference_means_nothing_is_released(self):
        self.capture("alpha", "timeout alpha: a note about the timeout.")
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        store = self.reader_store(reader)
        slow = store.reranker = SlowReranker()
        box = {}

        def run():
            command = {
                "version": 1,
                "incarnation": store.incarnation,
                "action": "brief",
                "args": self.brief_args(rerank="on"),
            }
            try:
                out = []
                Retrieval(store).execute(reader, command).emit(out.append)
                box["result"] = out[0]
            except BaseException as exc:
                box["error"] = exc

        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(slow.entered.wait(30))
            self.store.revoke(self.owner, "reader", "p")
        finally:
            slow.release.set()
            thread.join(60)
        self.assertNotIn("result", box)
        self.assertIsInstance(box.get("error"), LocalError)
        self.assertIn(box["error"].code, {"denied", "credential_replaced"})

    def test_visibility_costs_the_same_however_many_candidates_there_are(self):
        def cost():
            counted = Counting(self.store.db)
            self.store.db = counted
            try:
                result = self.run_r()
            finally:
                self.store.db = counted.delegate
            holds = sum("observation_holds" in q for q in counted.log)
            grants = sum("FROM grants" in q for q in counted.log)
            return len(result["guidance"]), holds, grants

        for number in range(5):
            self.capture(f"few{number}", f"timeout note {number} about the deploy")
        few = cost()
        for number in range(60):
            self.capture(f"many{number}", f"timeout note {number} about the rollout")
        many = cost()
        self.assertGreater(many[0], few[0])
        self.assertEqual(few[1:], many[1:])
        self.assertLessEqual(many[1], 2)


class LocalBriefingTests(BriefingCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown


class PostgresBriefingTests(BriefingCases, pg.NativeFixture):
    def reader_store(self, credential):
        from memoryking.adapters.postgres_store import PostgresStore

        store = PostgresStore(self.pool, self.data, self.control, self.tenant, credential)
        self.addCleanup(store.close)
        return store

    def shared(self):
        return SharedService(self.pool, self.data, self.control)

    def request(self, operation, args, credential=None):
        result = self.shared().execute(
            self.tenant,
            credential or self.owner,
            {"version": 1, "operation": operation, "args": args},
            time.monotonic() + 20,
        )
        if isinstance(result, Released):
            out = []
            result.emit(out.append)
            import json

            return json.loads(out[0])
        return result

    def brief_request(self):
        return self.request(
            "retrieval",
            {
                "command": {
                    "version": 1,
                    "incarnation": self.store.incarnation,
                    "action": "brief",
                    "args": self.brief_args(),
                }
            },
        )

    def test_briefings_and_captures_share_the_gate_while_policy_changes_wait_for_both(self):
        started, release = threading.Event(), threading.Event()
        original = Retrieval._brief

        def slow(this, *args, **kwargs):
            if not started.is_set():
                started.set()
                self.assertTrue(release.wait(30))
            return original(this, *args, **kwargs)

        results = {}

        def first():
            try:
                results["first"] = self.brief_request()
            except BaseException as exc:
                results["first"] = exc

        revoked = threading.Event()

        def revoke():
            try:
                with self.shared().store(self.tenant, self.owner) as store:
                    store.grant(self.owner, "probe", "p", ["read"])
                    store.revoke(self.owner, "probe", "p")
            finally:
                revoked.set()

        from unittest.mock import patch

        with patch.object(Retrieval, "_brief", slow):
            holder = threading.Thread(target=first)
            holder.start()
            try:
                self.assertTrue(started.wait(30))
                # A second briefing and a capture finish while the first holds the gate.
                begun = time.monotonic()
                self.assertEqual("partial", self.brief_request()["status"])
                self.request(
                    "capture",
                    {
                        "namespace": self.ns,
                        "key": "during",
                        "request": observation(9, project="p", text="timeout during a briefing"),
                    },
                )
                self.assertLess(time.monotonic() - begun, 10)
                # A policy change is excluded until the first briefing has finished, and a
                # briefing that arrives after it queues behind it, so a steady stream of
                # briefings cannot keep it waiting.
                policy = threading.Thread(target=revoke)
                policy.start()
                self.assertFalse(revoked.wait(1.0))
                late = threading.Thread(target=lambda: results.update(late=self.brief_request()))
                late.start()
                late.join(1.0)
                self.assertTrue(late.is_alive())
                self.assertFalse(revoked.is_set())
            finally:
                release.set()
                holder.join(60)
        policy.join(60)
        late.join(60)
        self.assertTrue(revoked.is_set())
        self.assertEqual("partial", results["late"]["status"])
        self.assertNotIsInstance(results["first"], BaseException)

    def test_a_connection_is_checked_once_and_again_when_its_age_passes(self):
        from memoryking.adapters import postgres_pool

        with self.pool.connection() as connection:
            statements = []
            real = connection.execute

            def spy(query, *args, **kwargs):
                statements.append(query)
                return real(query, *args, **kwargs)

            connection.execute = spy
            try:
                connection.memoryking_checked = None
                self.assertTrue(postgres_pool.checked(connection))
                first = len(statements)
                self.assertGreaterEqual(first, 5)
                self.assertFalse(postgres_pool.checked(connection))
                self.assertEqual(first, len(statements))
                self.assertTrue(postgres_pool.checked(connection, max_age=0.0))
                self.assertEqual(2 * first, len(statements))
            finally:
                del connection.execute
            # A session that stopped being durable is refused the moment it is looked at.
            connection.execute("SET synchronous_commit=off")
            with self.assertRaises(LocalError) as refused:
                postgres_pool.checked(connection, max_age=0.0)
            self.assertEqual("database_durability_required", refused.exception.code)
            postgres_pool.reset(connection)
            # The pool's reset restores the setting, so the next request finds it durable.
            self.assertEqual("on", connection.execute("SHOW synchronous_commit").fetchone()[0])


class AnchorTests(unittest.TestCase):
    """What the anchor protects, and when it is written (see `LocalStore._expose_locked`)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mk-an-", dir="/tmp")
        self.base = Path(self.temp.name)
        self.root, self.control = self.base / "db", self.base / "ctl"
        self.credential = LocalStore.initialize(self.root, self.control).read_text()
        self.clock = FakeClock()
        self.writes = []
        self.store = self.open()
        self.namespace = self.store.namespace(self.credential, "demo")

    def open(self):
        return LocalStore(
            self.root,
            self.control,
            clock=self.clock,
            fault=lambda point: self.writes.append(point) if point == "before_anchor" else None,
        )

    def tearDown(self):
        if self.store:
            self.store.close()
        self.temp.cleanup()

    def capture(self, number):
        return self.store.capture(
            self.credential,
            self.namespace,
            f"k{number}",
            observation(number, text="durable memory"),
        )

    def brief(self):
        command = {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": "brief",
            "args": {
                "project": "demo",
                "query": "durable",
                "bindings": [{}],
                "max_output_tokens": 2000,
                "tokenizer": "cl100k_base",
            },
        }
        out = []
        Retrieval(self.store).execute(self.credential, command).emit(out.append)
        return out[0]

    def anchor(self):
        return Control(self.control).read_anchor()

    def serial(self):
        return int(
            self.store.db.execute("SELECT value FROM meta WHERE key='commit_serial'").fetchone()[0]
        )

    def test_a_capture_is_anchored_before_it_is_acknowledged(self):
        base = len(self.writes)
        for number in range(1, 4):
            result = self.capture(number)
            self.assertEqual(result["position"], self.anchor()["frontiers"]["main"])
        self.assertEqual(base + 3, len(self.writes))

    def test_reads_that_change_no_history_write_no_anchor_until_the_interval_passes(self):
        self.capture(1)
        written, serial = len(self.writes), self.anchor()["commit_serial"]
        for _ in range(3):
            self.brief()
        self.store.grants(self.credential)
        self.store.status(self.credential, "demo")
        self.assertEqual(written, len(self.writes))
        self.assertEqual(serial, self.anchor()["commit_serial"])
        self.assertGreater(self.serial(), serial)  # the receipts are committed, the anchor lags
        self.clock.advance(ANCHOR_INTERVAL_MS)
        self.brief()
        self.assertEqual(written + 1, len(self.writes))
        self.assertEqual(self.serial(), self.anchor()["commit_serial"])
        self.assertEqual(self.store.last_utc, self.anchor()["last_utc"])

    def test_a_request_that_changed_nothing_commits_nothing(self):
        self.capture(1)
        serial = self.serial()
        self.store.grants(self.credential)
        self.assertEqual(serial, self.serial())
        with self.assertRaises(LocalError):
            self.store.read("not-a-token", "demo")
        self.assertEqual(serial, self.serial())

    def test_close_writes_what_was_left_for_later(self):
        self.capture(1)
        self.brief()
        lagging = self.anchor()["commit_serial"]
        serial = self.serial()
        self.assertLess(lagging, serial)
        self.store.close()
        self.store = None
        self.assertEqual(serial, self.anchor()["commit_serial"])

    def test_one_anchor_write_covers_the_commits_made_before_it(self):
        expose = self.store._expose
        self.store._expose = lambda: None  # two writers have committed, neither has exposed yet
        first, second = self.capture(1), self.capture(2)
        self.store._expose = expose
        self.assertLess(self.anchor()["frontiers"].get("main", 0), second["position"])
        written = len(self.writes)
        self.store._expose()  # the first writer's turn: one write covers both
        self.assertEqual(written + 1, len(self.writes))
        self.assertEqual(second["position"], self.anchor()["frontiers"]["main"])
        self.store._expose()  # the second writer finds its position covered
        self.assertEqual(written + 1, len(self.writes))
        self.assertLess(first["position"], second["position"])

    def test_concurrent_writers_never_acknowledge_ahead_of_the_anchor(self):
        base, failures = len(self.writes), []
        numbers = iter(range(1, 1000))
        lock = threading.Lock()

        def writer():
            for _ in range(6):
                with lock:
                    number = next(numbers)
                result = self.capture(number)
                if self.anchor()["frontiers"]["main"] < result["position"]:
                    failures.append((number, result["position"]))

        threads = [threading.Thread(target=writer) for _ in range(8)]
        [thread.start() for thread in threads]
        [thread.join(120) for thread in threads]
        self.assertEqual([], failures)
        self.assertEqual(48, self.anchor()["frontiers"]["main"])
        # Writers whose commits preceded another's anchor write shared it.
        self.assertLessEqual(len(self.writes) - base, 48)

    def snapshot(self, name):
        target = self.base / name
        target.mkdir()
        self.store.close()
        self.store = None
        for path in self.root.glob("store.sqlite*"):
            shutil.copy2(path, target / path.name)
        return target

    def roll_back_to(self, snapshot):
        for path in self.root.glob("store.sqlite*"):
            path.unlink()
        for path in snapshot.iterdir():
            shutil.copy2(path, self.root / path.name)

    def test_a_database_rolled_back_behind_an_anchored_write_is_refused(self):
        self.capture(1)
        before = self.snapshot("before")
        self.store = self.open()
        self.capture(2)
        self.store.close()
        self.store = None
        self.roll_back_to(before)
        with self.assertRaises(LocalError) as refused:
            self.open()
        self.assertEqual("recovery_required", refused.exception.code)

    def test_a_database_rolled_back_behind_receipts_is_refused_once_they_are_anchored(self):
        self.capture(1)
        before = self.snapshot("before")
        self.store = self.open()
        for _ in range(3):
            self.brief()
        self.store.close()  # close writes the commit serial the receipts advanced
        self.store = None
        self.roll_back_to(before)
        with self.assertRaises(LocalError) as refused:
            self.open()
        self.assertEqual("recovery_required", refused.exception.code)

    def test_the_clock_high_water_mark_is_never_more_than_the_interval_behind(self):
        self.capture(1)
        for _ in range(5):
            self.clock.advance(ANCHOR_INTERVAL_MS // 4)
            self.brief()
            self.assertLess(self.store.last_utc - self.anchor()["last_utc"], ANCHOR_INTERVAL_MS)
        self.store.close()
        self.store = self.open()
        # A clock that went back past what was anchored is refused; the old instant is not reused.
        self.clock.utc = self.anchor()["last_utc"] - 1
        with self.assertRaises(LocalError) as refused:
            self.brief()
        self.assertEqual("clock_untrusted", refused.exception.code)

    def run_script(self, point, body):
        script = f"""
import os, sys
from pathlib import Path
from memoryking.adapters.local_store import LocalStore
root, ctl, point = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
def fail(stage):
    if stage == point: os._exit(73)
store = LocalStore(root, ctl, fault=fail)
credential = (ctl / 'owner.credential').read_text()
{body}
os._exit(0)
"""
        return subprocess.run(
            [sys.executable, "-c", script, str(self.root), str(self.control), point],
            capture_output=True,
            env={k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}},
        )

    def test_killed_between_commit_and_anchor_loses_no_acknowledged_write(self):
        self.store.close()
        self.store = None
        for point in ("after_commit", "before_anchor", "after_anchor"):
            with self.subTest(point=point):
                root, control = self.base / f"{point}-db", self.base / f"{point}-ctl"
                credential = LocalStore.initialize(root, control).read_text()
                with LocalStore(root, control) as store:
                    namespace = store.namespace(credential, "demo")
                    store.capture(credential, namespace, "first", observation(1))
                self.root, self.control = root, control
                body = (
                    f"store.capture(credential, {namespace!r}, 'second', "
                    "{'version':1,'project':'demo','source':'test','source_event':'e2',"
                    "'text':'second','readers':None,'classification':'internal','observed_at_ms':0})"
                )
                self.assertEqual(73, self.run_script(point, body).returncode)
                with LocalStore(root, control) as store:
                    # The commit was durable before the anchor was reached: the database is
                    # ahead of the anchor, never behind it, and the retry replays.
                    self.assertEqual(
                        2, store.db.execute("SELECT count(*) FROM observations").fetchone()[0]
                    )
                    again = store.capture(
                        credential, namespace, "second", observation(2, text="second")
                    )
                    self.assertEqual(2, again["position"])
                    self.assertEqual(
                        2, store.db.execute("SELECT count(*) FROM observations").fetchone()[0]
                    )

    def test_killed_after_a_briefing_the_receipt_survives_and_replays(self):
        self.store.close()
        self.store = None
        root, control = self.base / "kill-db", self.base / "kill-ctl"
        credential = LocalStore.initialize(root, control).read_text()
        with LocalStore(root, control) as store:
            store.capture(credential, store.namespace(credential, "demo"), "k", observation(1))
        self.root, self.control = root, control
        body = (
            "import json\n"
            "from memoryking.adapters.retrieval import Retrieval\n"
            "command = {'version':1,'incarnation':store.incarnation,'action':'brief','args':{"
            "'project':'demo','query':'durable','bindings':[{}],'max_output_tokens':2000,"
            "'tokenizer':'cl100k_base'}}\n"
            "out = []\n"
            "Retrieval(store).execute(credential, command).emit(out.append)\n"
            "(ctl / 'receipt.json').write_text(json.dumps({'body': out[0].decode(), "
            "'receipt': json.loads(out[0])['receipt']}))\n"
        )
        # No close: the anchor still lags the receipt, and the store must open and replay it.
        done = self.run_script("none", body)
        self.assertEqual(0, done.returncode, done.stderr.decode())
        import json

        saved = json.loads((control / "receipt.json").read_text())
        (control / "receipt.json").unlink()
        with LocalStore(root, control) as store:
            command = {
                "version": 1,
                "incarnation": store.incarnation,
                "action": "replay",
                "args": {"project": "demo", "receipt": saved["receipt"]},
            }
            out = []
            Retrieval(store).execute(credential, command).emit(out.append)
            self.assertEqual(saved["body"], out[0].decode())


class PostgresAnchorTests(pg.NativeFixture):
    """The shared profile writes the anchor on the same rules."""

    def setUp(self):
        super().setUp()
        self.points = []

    def store_for(self):
        from memoryking.adapters.postgres_store import PostgresStore

        return PostgresStore(
            self.pool,
            self.data,
            self.control,
            self.tenant,
            self.owner,
            fault=lambda point: self.points.append(point) if point == "before_anchor" else None,
        )

    def anchor(self):
        return SharedControl(self.control / self.tenant).read_anchor()

    def test_a_capture_is_anchored_and_a_briefing_is_not(self):
        with self.store_for() as store:
            before = self.anchor()["frontiers"][self.tenant]
            namespace = store.namespace(self.owner, "demo")
            result = store.capture(
                self.owner, namespace, "k1", observation(1, project="demo", text="timeout memory")
            )
            self.assertEqual(result["position"], self.anchor()["frontiers"][self.tenant])
            self.assertGreater(result["position"], before)
        anchored = len(self.points)
        self.assertGreaterEqual(anchored, 1)
        serial = self.anchor()["commit_serial"]
        with self.store_for() as store:
            command = {
                "version": 1,
                "incarnation": store.incarnation,
                "action": "brief",
                "args": {
                    "project": "demo",
                    "query": "timeout",
                    "bindings": [{}],
                    "max_output_tokens": 2000,
                    "tokenizer": "cl100k_base",
                },
            }
            out = []
            Retrieval(store).execute(self.owner, command).emit(out.append)
            self.assertIn(b"timeout memory", out[0])
        # The briefing named a position the anchor already covered, so it wrote nothing new
        # (it may be the first write of the interval for the commit serial, never for history).
        self.assertLessEqual(len(self.points), anchored + 1)
        self.assertEqual(
            self.anchor()["frontiers"][self.tenant],
            result["position"],
        )
        self.assertGreaterEqual(self.anchor()["commit_serial"], serial)

    def test_a_request_that_wrote_nothing_takes_no_commit_serial(self):
        query = "SELECT value FROM tenant_meta WHERE key='commit_serial'"
        serial = self.store.db.execute(query).fetchone()[0]
        with self.store_for() as store:
            store.grants(self.owner)
        self.assertEqual(serial, self.store.db.execute(query).fetchone()[0])
        with self.store_for() as store:
            store.namespace(self.owner, "demo")
            store.capture(
                self.owner,
                store.namespace(self.owner, "demo"),
                "k",
                observation(1, project="demo"),
            )
        self.assertNotEqual(serial, self.store.db.execute(query).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
