"""Cross-backend release races and independently exercised temporal predicates."""

import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import test_knowledge
import test_retrieval
from test_local_store import FakeClock
from test_postgres_parity import NativeFixture, reuse

from memoryking.adapters.postgres_store import PostgresStore
from memoryking.adapters.retrieval import Retrieval
from memoryking.domain.local import LocalError
from memoryking.domain.selectors import Work, canonical, contains, intersection


def materialize(packet):
    output = []
    packet.emit(output.append)
    return json.loads(output[0])


class ReleaseCases:
    def test_selector_overlap_unknown_and_rejected_predicates(self):
        region, sel, eq = test_knowledge.region, test_knowledge.sel, test_knowledge.eq
        old = region("payments", ["production", "staging"], [1, 0, 0], [3, 0, 0])
        new = region("payments/worker", ["production"], [2, 0, 0], [4, 0, 0])
        overlap = intersection(old, new)
        self.assertEqual(
            canonical(region("payments/worker", ["production"], [2, 0, 0], [3, 0, 0])), overlap
        )
        self.assertEqual("proven", contains(overlap, old))
        original = self.review(self.propose(self.body("predecessor", selector=old)))
        successor = self.propose(self.body("successor", selector=new), version=2)
        self.review(
            successor,
            replacement={
                "predecessor": original["revision"],
                "selector": overlap,
                "valid_start": None,
                "valid_end": None,
            },
        )
        binding = {
            "repository_id": "repo",
            "path": "payments/worker/x",
            "environment": "production",
            "dependencies": {"lib": [2, 5, 0]},
        }
        self.assertEqual(
            [successor["revision"]],
            [v["revision"] for v in self.query(bindings=[binding])["items"]],
        )
        self.assertEqual(
            [original["revision"]],
            [
                v["revision"]
                for v in self.query(bindings=[{**binding, "environment": "staging"}])["items"]
            ],
        )
        for missing in ({"environment": None}, {"dependencies": {}}, {"path": None}):
            result = self.query(bindings=[{**binding, **missing}])
            self.assertEqual("blocked", result["status"])
            self.assertEqual([], result["items"])
        self.assertEqual(
            [], self.query(bindings=[{**binding, "dependencies": {"lib": None}}])["items"]
        )
        for invalid in (
            sel({"op": "exec", "value": "untrusted()"}),
            {"selector_version": "1", "any_of": [{"all_of": [{"op": "any"}]}] * 17},
        ):
            before = self.store.db.execute("SELECT count(*) FROM revisions").fetchone()[0]
            with self.assertRaises(LocalError) as caught:
                self.propose(self.body(selector=invalid), key="invalid")
            self.assertIn(
                caught.exception.code,
                {"invalid_request", "selector_unsupported", "selector_limit_exceeded"},
            )
            self.assertEqual(
                before, self.store.db.execute("SELECT count(*) FROM revisions").fetchone()[0]
            )
        with self.assertRaises(LocalError) as caught:
            intersection(old, new, Work(0))
        self.assertEqual("selector_limit_exceeded", caught.exception.code)
        ancestor = sel(
            eq("repository_id", "repo"),
            {"field": "commit", "op": "descendant_of", "value": "a" * 40},
        )
        other = sel(
            eq("repository_id", "repo"),
            {"field": "commit", "op": "descendant_of", "value": "b" * 40},
        )
        self.assertEqual("indeterminate", contains(ancestor, other))
        policy = self.propose(
            self.body(
                "mandatory ancestry",
                selector=ancestor,
                kind="constraint",
                authority="project",
                rule_key="ancestry",
                effect="require",
                rule_value="required",
            ),
            key="ancestry",
        )
        self.review(policy)
        result = self.constraints(bindings=[{**binding, "commit": "b" * 40}])
        self.assertEqual("blocked", result["status"])
        self.assertEqual("applicability_unknown", result["reason"])

    def test_durable_replacement_inventory_cannot_exhaust_proof_silently(self):
        original = self.review(self.propose())
        successor = self.propose(self.body("replacement"), version=2)
        self.review(
            successor,
            replacement={
                "predecessor": original["revision"],
                "selector": test_knowledge.sel(),
                "valid_start": None,
                "valid_end": None,
            },
        )
        # A durable saturation fixture, using each backend's real transaction and event clock.
        # No predicate/authorization mock replaces the subsequent product query.
        with self.store.transaction():
            edge = self.store.db.execute("SELECT * FROM replacements").fetchone()
            columns = list(edge.keys())
            for _ in range(300):
                values = list(edge)
                values[columns.index("position")] = self.store._event(
                    edge["tenant"],
                    "review",
                    original["revision"],
                    self.store.clock.utc_ms(),
                    "owner",
                )
                self.store.db.execute("INSERT INTO replacements VALUES (?,?,?,?,?,?,?)", values)
        result = self.query(bindings=[{} for _ in range(256)])
        self.assertEqual("blocked", result["status"])
        self.assertEqual("selector_limit_exceeded", result["reason"])
        self.assertEqual([], result["items"])

    def test_export_cursor_expiry_and_backward_clock(self):
        clock = self.fixed_clock()
        for n in range(32):
            self.store.capture(
                self.owner,
                self.ns,
                f"export-{n}",
                {
                    "version": 1,
                    "project": "p",
                    "source": "matrix",
                    "source_event": str(n),
                    "text": f"export record {n}",
                    "readers": None,
                    "classification": "internal",
                    "observed_at_ms": 0,
                },
            )
        start = clock.utc_ms()
        reader = self.store.grant(
            self.owner, "exporter", "p", ["read", "export"], expires=start + 1000
        )
        with self.reader(reader) as store:
            store.fault = lambda point: clock.advance(4999) if point == "after_validation" else None
            packet = store.read(reader, "p", export=True)
            value = materialize(packet)
            self.assertEqual(start, value["evaluated_ms"])
            self.assertIsNotNone(value["cursor"])
            store.fault = lambda _: None
            with self.assertRaises(LocalError) as caught:
                store.read(reader, "p", export=True, cursor=value["cursor"])
            self.assertEqual("denied", caught.exception.code)
            clock.advance(2)
            output = []
            with self.assertRaises(LocalError) as caught:
                packet.emit(output.append)
            self.assertEqual("release_expired", caught.exception.code)
            self.assertEqual([], output)
        packet = self.store.read(self.owner, "p", export=True)
        clock.utc -= 1
        output = []
        with self.assertRaises(LocalError) as caught:
            packet.emit(output.append)
        self.assertEqual("clock_untrusted", caught.exception.code)
        self.assertEqual([], output)

    def packet(self, store, credential, surface, receipt=None, **kw):
        if surface == "export":
            return store.read(credential, "p", export=True)
        command = {
            "version": 1,
            "incarnation": store.incarnation,
            "action": "replay" if surface == "replay" else "brief",
            "args": {"project": "p", "receipt": receipt}
            if surface == "replay"
            else self.brief_args(**kw),
        }
        return Retrieval(store).execute(credential, command)

    def fixed_clock(self):
        clock = FakeClock()
        clock.utc = self.store.clock.utc_ms() + 1
        self.store.clock = clock
        return clock

    def test_release_races_compile_cache_replay_export(self):
        self.fixed_clock()
        self.review(self.propose())
        for surface in ("compile", "cache", "replay", "export"):
            with self.subTest(surface=surface):
                reader = self.store.grant(self.owner, "r_" + surface, "p", ["read", "export"])
                with self.reader(reader) as store:
                    prior = materialize(self.packet(store, reader, "compile"))
                    self.assertIn("timeout=30", json.dumps(prior))
                    if surface == "cache":
                        self.assertTrue(store.retrieval_cache)
                    # A reduction that commits before final authorization prevents release,
                    # including a warm candidate cache and an already materialized packet.
                    self.store.revoke(self.owner, "r_" + surface, "p")
                    with self.assertRaises(LocalError) as caught:
                        self.packet(store, reader, surface, prior["receipt"])
                    self.assertEqual("denied", caught.exception.code)
                    self.store.grant(self.owner, "r_" + surface, "p", ["read", "export"])
                    # The previous materialization is no longer reusable after an epoch change.
                    prior = materialize(self.packet(store, reader, "compile"))
                    validated, proceed, revoking, revoked = (threading.Event() for _ in range(4))
                    output = []

                    def pause(point):
                        if point == "after_validation" and not validated.is_set():
                            validated.set()
                            if not proceed.wait(3):
                                raise RuntimeError("Test did not release validation pause")

                    def revoke():
                        revoking.set()
                        self.store.revoke(self.owner, "r_" + surface, "p")
                        revoked.set()

                    store.fault = pause
                    with ThreadPoolExecutor(2) as executor:
                        reading = executor.submit(
                            self.packet, store, reader, surface, prior["receipt"]
                        )
                        try:
                            self.assertTrue(validated.wait(3))
                            revocation = executor.submit(revoke)
                            self.assertTrue(revoking.wait(1))
                            self.assertFalse(revoked.wait(0.05))
                            self.assertEqual([], output)
                        finally:
                            proceed.set()
                        released = reading.result(3)
                        revocation.result(3)
                    store.fault = lambda _: None
                    self.assertTrue(revoked.is_set())
                    released.emit(output.append)
                    self.assertIn(b"timeout=30", output[0])
                    with self.assertRaises(LocalError) as caught:
                        self.packet(store, reader, surface, prior["receipt"])
                    self.assertEqual("denied", caught.exception.code)

    def test_each_expiry_predicate_at_4999_and_5001_ms(self):
        clock = self.fixed_clock()
        for boundary in ("grant", "constraint_start", "constraint_end", "verification"):
            for delay in (4999, 5001):
                with self.subTest(boundary=boundary, delay=delay):
                    marker = f"timeout-{boundary}-{delay}"
                    start = clock.utc_ms()
                    expires = start + 1000
                    subject = f"r_{boundary}_{delay}"
                    reader = self.store.grant(
                        self.owner,
                        subject,
                        "p",
                        ["read"],
                        expires=expires if boundary == "grant" else None,
                    )
                    if boundary.startswith("constraint"):
                        claim = self.propose(
                            self.body(
                                marker,
                                kind="constraint",
                                authority="project",
                                rule_key=marker,
                                effect="require",
                                rule_value="required",
                                valid_start=expires if boundary == "constraint_start" else None,
                                valid_end=expires
                                if boundary == "constraint_end"
                                else expires + 10000,
                            ),
                            key=marker,
                        )
                    else:
                        claim = self.propose(self.body(marker), key=marker)
                    self.review(claim)
                    if boundary == "verification":
                        self.run_k(
                            "attest",
                            {
                                "project": "p",
                                "revision": claim["revision"],
                                "statement": "Synthetic attributed verification",
                                "expires_ms": expires,
                            },
                        )
                    with self.reader(reader) as store:
                        triggered = []

                        def stall(point):
                            if point == "after_validation" and not triggered:
                                triggered.append(point)
                                clock.advance(delay)

                        store.fault = stall
                        options = {"query": marker}
                        if boundary == "verification":
                            options["freshness"] = "attested"
                        output = []
                        if delay == 5001:
                            with self.assertRaises(LocalError) as caught:
                                self.packet(store, reader, "compile", **options).emit(output.append)
                            self.assertEqual("release_expired", caught.exception.code)
                            self.assertEqual([], output)
                        else:
                            released = self.packet(store, reader, "compile", **options)
                            released.emit(output.append)
                            value = json.loads(output[0])
                            self.assertEqual(start, value["evaluated_ms"])
                            texts = [v["text"] for v in value["constraints"] + value["guidance"]]
                            self.assertEqual(boundary != "constraint_start", marker in texts)
                            clock.advance(2)
                            with self.assertRaises(LocalError) as caught:
                                released.emit(output.append)
                            self.assertEqual("release_expired", caught.exception.code)
                            self.assertEqual(1, len(output))
                        self.assertEqual(["after_validation"], triggered)
                        store.fault = lambda _: None
                        if boundary == "grant":
                            with self.assertRaises(LocalError) as caught:
                                self.packet(store, reader, "compile", **options)
                            self.assertEqual("denied", caught.exception.code)
                        else:
                            current = materialize(self.packet(store, reader, "compile", **options))
                            texts = [
                                v["text"] for v in current["constraints"] + current["guidance"]
                            ]
                            self.assertEqual(boundary == "constraint_start", marker in texts)


class SQLiteReleaseMatrix(ReleaseCases, unittest.TestCase):
    setUp = test_knowledge.KnowledgeTests.setUp
    tearDown = test_knowledge.KnowledgeTests.tearDown

    @contextmanager
    def reader(self, credential):
        yield self.store


reuse(
    SQLiteReleaseMatrix,
    test_knowledge.KnowledgeTests,
    "body propose review run_k query constraints",
)
reuse(SQLiteReleaseMatrix, test_retrieval.RetrievalTests, "brief_args")


class PostgresReleaseMatrix(ReleaseCases, NativeFixture):
    @contextmanager
    def reader(self, credential):
        with PostgresStore(
            self.pool, self.data, self.control, self.tenant, credential, clock=self.store.clock
        ) as store:
            yield store
