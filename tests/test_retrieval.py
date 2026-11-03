"""Local product checks for full-envelope budgets, safe selection and exact replay."""

import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import test_knowledge as knowledge_helpers
from test_local_store import FakeClock

from memoryking.adapters.local_store import RETRY_MS
from memoryking.adapters.retrieval import Retrieval
from memoryking.adapters.token_count import encoding
from memoryking.contracts.canonical import ContractError
from memoryking.contracts.validation import validate_retrieval
from memoryking.domain.local import LocalError


class RetrievalTests(unittest.TestCase):
    setUp = knowledge_helpers.KnowledgeTests.setUp
    tearDown = knowledge_helpers.KnowledgeTests.tearDown
    run_k = knowledge_helpers.KnowledgeTests.run_k
    body = knowledge_helpers.KnowledgeTests.body
    propose = knowledge_helpers.KnowledgeTests.propose
    review = knowledge_helpers.KnowledgeTests.review

    def run_r(self, action="brief", args=None, credential=None, raw=False):
        command = {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": action,
            "args": args or self.brief_args(),
        }
        result = Retrieval(self.store).execute(credential or self.owner, command)
        data = []
        result.emit(data.append)
        return data[0] if raw else json.loads(data[0])

    def brief_args(self, **kw):
        return {
            "project": "p",
            "query": "timeout",
            "bindings": [{}],
            "max_output_tokens": 6000,
            "tokenizer": "cl100k_base",
            **kw,
        }

    def receipt(self, result):
        return self.run_r("receipt", {"project": "p", "receipt": result["receipt"]})

    def clock(self):
        clock = FakeClock()
        clock.utc = self.store.clock.utc_ms() + 1
        self.store.clock = clock
        return clock

    def fails(self, code, fn):
        with self.assertRaises(LocalError) as exc:
            fn()
        self.assertEqual(exc.exception.code, code)

    def constraint(self, text="Never disclose credentials", key="policy", **kw):
        proposed = self.propose(
            self.body(
                text=text,
                kind="constraint",
                authority="project",
                rule_key=key,
                rule_value="required",
                effect="require",
                **kw,
            ),
            key=key,
        )
        self.review(proposed)
        return proposed

    def test_as_of_brief_sees_only_what_was_recorded_by_then_and_replays(self):
        first_at = self.store.clock.utc_ms()
        clock = self.clock()
        clock.utc = first_at + 60_000
        later = self.store.capture(
            self.owner,
            self.ns,
            "later",
            {
                "version": 1,
                "project": "p",
                "source": "test",
                "source_event": "later",
                "text": "A later timeout finding recorded a minute afterwards.",
                "readers": None,
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )
        clock.utc = first_at + 120_000
        now = self.run_r()
        self.assertIn(later["observation"], [g["reference"] for g in now["guidance"]])
        then = self.run_r(args=self.brief_args(as_of_ms=first_at + 1))
        refs = [g["reference"] for g in then["guidance"]]
        self.assertIn(self.obs["observation"], refs)
        self.assertNotIn(later["observation"], refs)
        self.assertEqual(then["snapshot"]["as_of_ms"], first_at + 1)
        self.assertEqual(then["snapshot"]["valid_ms"], first_at + 1)
        self.assertLess(then["snapshot"]["recorded_position"], now["snapshot"]["recorded_position"])
        replay = self.run_r("replay", {"project": "p", "receipt": then["receipt"]})
        self.assertEqual(replay, then)
        text = self.run_r(args=self.brief_args(as_of_ms=first_at + 1, format="text"))
        self.assertIn("as of", text["text"].splitlines()[1])
        nothing = self.run_r(args=self.brief_args(as_of_ms=0))
        self.assertEqual(nothing["guidance"], [])
        self.fails(
            "invalid_request",
            lambda: self.run_r(args=self.brief_args(as_of_ms=first_at, recorded_position=0)),
        )

    def test_mandatory_survives_zero_similarity_and_top_k_zero(self):
        self.constraint()
        result = self.run_r(args=self.brief_args(query="astronomy", top_k=0))
        self.assertEqual(result["constraints"][0]["text"], "Never disclose credentials")
        self.assertEqual(result["guidance"], [])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(
            self.receipt(result)["diagnostics"]["selected"][0]["reason"], "mandatory_registry"
        )

    def test_budget_insufficient_is_explicit_and_no_truncated_mandatory_text(self):
        self.constraint("A compulsory condition " * 150)
        result = self.run_r(args=self.brief_args(max_output_tokens=500))
        self.assertEqual(result["status"], "budget_insufficient")
        self.assertGreater(result["minimum_required_tokens"], 500)
        self.assertEqual(result["constraints"], [])
        self.assertFalse(result["usage"]["budget_compliant"])

    def test_exact_final_envelope_unicode_and_special_token_literals(self):
        for query in ["timeout", "🔒 保持数据安全 café <|endoftext|>", "unknown topic"]:
            raw = self.run_r(args=self.brief_args(query=query, max_output_tokens=1000), raw=True)
            result = json.loads(raw)
            count = len(encoding().encode_ordinary(raw.decode()))
            self.assertLessEqual(count, 1000)
            self.assertIn(result["usage"]["output_tokens"], [count, None])
            self.assertEqual(self.receipt(result)["diagnostics"]["usage"]["output_tokens"], count)
            self.assertEqual(self.receipt(result)["rendered_hash"], hashlib.sha256(raw).hexdigest())

    def test_unknown_tokenizer_is_disclosed_and_no_provider_required(self):
        with patch(
            "memoryking.adapters.token_count.encoding",
            side_effect=AssertionError("no optional tokenizer"),
        ):
            result = self.run_r(
                args=self.brief_args(tokenizer="unknown-model", max_output_tokens=6000)
            )
        self.assertEqual(result["usage"]["counting"], "estimate")
        self.assertEqual(result["usage"]["provider_usage"], "unknown")
        self.assertIn("token_estimate_no_exact_guarantee", result["warnings"])

    def test_abstention_and_top_k_do_not_claim_absence(self):
        result = self.run_r(args=self.brief_args(query="astronomy nebula"))
        self.assertEqual(result["status"], "no_relevant_memory")
        self.assertEqual(result["guidance"], [])
        result = self.run_r(args=self.brief_args(top_k=0))
        self.assertEqual(result["status"], "partial")

    def test_empty_cache_new_claim_acceptance_and_membership(self):
        args = self.brief_args(query="zebras")
        first = self.run_r(args=args)
        self.assertEqual(first["status"], "no_relevant_memory")
        second = self.run_r(args=args)
        self.assertEqual(self.receipt(second)["diagnostics"]["cache"], "hit")
        claim = self.propose(self.body(text="zebras require careful migration"))
        self.review(claim)
        third = self.run_r(args=args)
        self.assertEqual(self.receipt(third)["diagnostics"]["cache"], "miss")
        self.assertIn(claim["revision"], [i["reference"] for i in third["guidance"]])

    def test_time_boundary_recomputes_policy_and_optional_eligibility(self):
        clock = self.clock()
        start = clock.utc + 1000
        self.constraint(valid_start=start, valid_end=start + 1000)
        self.assertEqual(self.run_r()["constraints"], [])

        clock.advance(1000)
        self.assertEqual(len(self.run_r()["constraints"]), 1)
        clock.advance(1000)
        self.assertEqual(self.run_r()["constraints"], [])

    def test_attestation_expiry_rechecks_freshness_on_cached_requests(self):
        clock = self.clock()
        policy = self.constraint()
        args = self.brief_args(freshness="attested")
        self.assertEqual(self.run_r(args=args)["reason"], "verification_unavailable")
        self.run_k(
            "attest",
            {
                "project": "p",
                "revision": policy["revision"],
                "expires_ms": clock.utc + 1000,
                "statement": "Human checked this policy",
            },
        )
        active = self.run_r(args=args)
        self.assertTrue(active["constraints"][0]["attestations"])
        clock.advance(1000)
        self.assertEqual(self.run_r(args=args)["reason"], "verification_unavailable")

    def test_conflict_expansion_does_not_compete_for_top_k(self):
        left = self.propose(self.body(text="alpha migration"))
        self.review(left)
        right = self.propose(self.body(text="beta procedure"), version=2)
        self.review(right)
        result = self.run_r(args=self.brief_args(query="alpha", top_k=1))
        refs = {i["reference"] for i in result["guidance"]}
        self.assertEqual(refs, {left["revision"], right["revision"]})
        self.assertTrue(all(i["conflicts"] for i in result["guidance"]))

    def test_hidden_policy_blocks_without_identifiers_or_counts(self):
        policy = self.constraint(readers=["owner"])
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        result = self.run_r(credential=reader)
        self.assertEqual(result["status"], "blocked")
        self.assertNotIn(policy["revision"], json.dumps(result))
        detail = self.run_r(
            "receipt", {"project": "p", "receipt": result["receipt"]}, credential=reader
        )
        self.assertEqual(detail["diagnostics"]["trace"], [])
        self.assertNotIn("Never disclose credentials", json.dumps(detail))

    def test_replay_original_bytes_and_separate_new_access_receipt(self):
        original = self.run_r(raw=True)
        receipt = json.loads(original)["receipt"]
        before = self.store.db.execute("SELECT count(*) FROM receipts").fetchone()[0]
        replay = self.run_r("replay", {"project": "p", "receipt": receipt}, raw=True)
        self.assertEqual(replay, original)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM receipts").fetchone()[0], before + 1
        )
        self.assertEqual(
            self.receipt(json.loads(original))["rendered_hash"], hashlib.sha256(replay).hexdigest()
        )

    def test_revocation_denies_cached_compilation_and_replay(self):
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        result = self.run_r(credential=reader)
        self.run_r(credential=reader)
        self.store.revoke(self.owner, "reader", "p")
        self.fails("denied", lambda: self.run_r(credential=reader))
        self.fails(
            "denied",
            lambda: self.run_r(
                "replay", {"project": "p", "receipt": result["receipt"]}, credential=reader
            ),
        )

    def test_source_purge_removes_retained_bytes_and_never_regenerates(self):
        result = self.run_r()
        unrelated = self.run_r(args=self.brief_args(query="astronomy"))
        self.store.purge(self.owner, self.obs["observation"])
        self.fails(
            "replay_purged",
            lambda: self.run_r("replay", {"project": "p", "receipt": result["receipt"]}),
        )
        row = self.store.db.execute(
            "SELECT * FROM rendered_artifacts WHERE id=?", (result["receipt"],)
        ).fetchone()
        self.assertIsNone(row["body"])
        self.assertIsNone(row["diagnostics"])
        # An artifact that recorded no dependency on the source keeps replaying.
        self.assertEqual(
            self.run_r("replay", {"project": "p", "receipt": unrelated["receipt"]})["receipt"],
            unrelated["receipt"],
        )
        self.assertIsNone(row["request_json"])

    def test_expired_missing_and_corrupt_are_distinct(self):
        clock = self.clock()
        result = self.run_r()
        args = {"project": "p", "receipt": result["receipt"]}
        self.store.db.execute("UPDATE rendered_artifacts SET body=?", (b"corrupt",))
        self.fails("replay_integrity_failure", lambda: self.run_r("replay", args))
        self.fails("replay_missing", lambda: self.run_r("replay", {**args, "receipt": "missing"}))
        clock.advance(RETRY_MS)
        self.fails("replay_expired", lambda: self.run_r("replay", args))
        rerun = self.run_r(
            "rerun",
            {
                "project": "p",
                "predecessor": result["receipt"],
                "brief": self.brief_args(query="fresh query"),
            },
        )
        self.assertNotEqual(rerun["receipt"], result["receipt"])

    def test_release_deadline_rolls_back_materialization(self):
        clock = self.clock()
        self.store.fault = lambda stage: (
            clock.advance(5001) if stage == "after_validation" else None
        )
        self.fails("release_expired", self.run_r)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM rendered_artifacts").fetchone()[0], 0
        )

    def test_evaluation_instant_and_first_byte_deadline(self):
        clock = self.clock()
        reader = self.store.grant(self.owner, "reader", "p", ["read"], expires=clock.utc + 100)
        self.store.fault = lambda stage: clock.advance(101) if stage == "after_validation" else None
        result = self.run_r(credential=reader)
        self.assertTrue(result["guidance"])
        self.store.fault = lambda _: None
        self.fails("denied", lambda: self.run_r(credential=reader))
        packet = Retrieval(self.store).execute(
            self.owner,
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": "brief",
                "args": self.brief_args(),
            },
        )
        clock.advance(5000)
        self.fails("release_expired", lambda: packet.emit(lambda _: self.fail("released")))

    def test_watermark_and_required_channel_degradation(self):
        consistency = {
            "at_least_watermark": self.obs["watermark"],
            "required_channels": ["lexical"],
        }
        result = self.run_r(args=self.brief_args(consistency=consistency))
        diag = self.receipt(result)["diagnostics"]
        self.assertGreaterEqual(diag["source_position"], self.obs["position"])
        self.assertEqual(diag["channel_frontiers"]["lexical"]["position"], diag["source_position"])
        consistency.update(required_channels=["dense"], wait_ms=2000)
        self.fails(
            "index_not_ready", lambda: self.run_r(args=self.brief_args(consistency=consistency))
        )
        consistency["on_timeout"] = "partial"
        self.assertEqual(
            self.run_r(args=self.brief_args(consistency=consistency))["coverage"][
                "required_missing"
            ],
            ["dense"],
        )

    def test_bounded_fetch_and_explicit_rerun(self):
        args = {
            "project": "p",
            "kind": "observation",
            "reference": self.obs["observation"],
            "start_byte": 0,
            "end_byte": 8,
            "max_output_tokens": 2000,
            "tokenizer": "cl100k_base",
        }
        result = self.run_r("fetch", args)
        self.assertEqual(result["excerpt"], self.raw[:8])
        self.fails("invalid_range", lambda: self.run_r("fetch", {**args, "end_byte": 65000}))
        new = self.run_r(
            "rerun",
            {
                "project": "p",
                "predecessor": result["receipt"],
                "brief": self.brief_args(query="unrelated"),
            },
        )
        self.assertNotEqual(result["receipt"], new["receipt"])
        self.assertEqual(self.receipt(new)["diagnostics"]["predecessor"], result["receipt"])

    def test_restore_rejects_old_history_and_retained_artifacts(self):
        from recovery_helpers import backup_fixture, restore_fixture

        result = self.run_r()
        old_inc = self.store.incarnation
        restore_fixture(self, backup_fixture(self))
        self.fails(
            "history_changed",
            lambda: Retrieval(self.store).execute(
                self.owner,
                {
                    "version": 1,
                    "incarnation": old_inc,
                    "action": "brief",
                    "args": self.brief_args(),
                },
            ),
        )
        self.fails(
            "history_changed",
            lambda: self.run_r("replay", {"project": "p", "receipt": result["receipt"]}),
        )
        self.assertEqual(self.store.retrieval_cache, {})

    def test_shared_request_vectors_and_query_bounds(self):
        vectors = json.loads(
            (
                Path(__file__).resolve().parents[1] / "fixtures/contracts/retrieval-vectors.json"
            ).read_text()
        )
        for vector in vectors:
            if vector["valid"]:
                validate_retrieval(vector["request"])
            else:
                with self.assertRaises(ContractError):
                    validate_retrieval(vector["request"])
        # A long query is searched on its leading terms and the cut is disclosed.
        long = self.run_r(args=self.brief_args(query=" ".join(f"word{i}" for i in range(40))))
        self.assertEqual(self.receipt(long)["diagnostics"]["query_terms_truncated"], 32)
        self.assertNotIn("query_terms_truncated", self.receipt(self.run_r())["diagnostics"])

    def test_hidden_conflict_endpoint_is_generic_incomplete(self):
        left = self.propose(self.body(text="alpha visible guidance"))
        self.review(left)
        right = self.propose(self.body(text="private disagreement", readers=["owner"]), version=2)
        self.review(right)
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        result = self.run_r(args=self.brief_args(query="alpha"), credential=reader)
        selected = next(i for i in result["guidance"] if i["reference"] == left["revision"])
        self.assertEqual(selected["conflict_resolution"], "incomplete")
        self.assertNotIn(right["revision"], json.dumps(result))
        self.assertNotIn("private disagreement", json.dumps(result))

    def observe(self, key, text, observed_at_ms=1):
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
                "observed_at_ms": observed_at_ms,
            },
        )

    def test_multi_word_query_matches_any_term_on_every_backend(self):
        captured = self.observe("deadline", "retry backoff exceeded the total deadline")
        self.observe("unrelated", "astronomy lecture notes")
        result = self.run_r(args=self.brief_args(query="deadline nonsenseterm"))
        references = [i["reference"] for i in result["guidance"]]
        self.assertIn(captured["observation"], references)
        self.assertEqual(len(references), 1)
        result = self.run_r(args=self.brief_args(query="nonsenseterm deadline retry"))
        self.assertIn(captured["observation"], [i["reference"] for i in result["guidance"]])

    def test_text_format_renders_cited_markdown_with_the_same_receipt_semantics(self):
        self.constraint()
        captured = self.observe("deadline", "retry backoff exceeded the total deadline")
        result = self.run_r(args=self.brief_args(query="deadline", format="text"))
        self.assertEqual(result["format"], "text")
        self.assertEqual(result["status"], "partial")
        text = result["text"]
        self.assertTrue(text.startswith("# Briefing: deadline"))
        self.assertIn("## Must follow", text)
        self.assertIn("Never disclose credentials", text)
        self.assertIn("## Guidance", text)
        self.assertIn("[g1]", text)
        self.assertIn("retry backoff exceeded the total deadline", text)
        self.assertIn("## Sources", text)
        self.assertIn(captured["observation"], text)
        self.assertIn("## Caveats", text)
        refs = {s["id"] for s in result["sources"]}
        self.assertIn(captured["observation"], refs)
        self.assertNotIn("guidance", result)
        self.assertGreater(result["usage"]["output_tokens"], 0)
        # Replay returns the exact envelope bytes; the receipt records the text format.
        raw = self.run_r(args=self.brief_args(query="deadline", format="text"), raw=True)
        replayed = self.run_r("replay", {"project": "p", "receipt": result["receipt"]}, raw=True)
        self.assertEqual(json.loads(replayed)["receipt"], result["receipt"])
        self.assertEqual(json.loads(replayed)["text"], result["text"])
        self.assertEqual(self.receipt(result)["diagnostics"]["format"], "text")
        self.assertEqual(
            self.receipt(result)["diagnostics"]["usage"]["output_tokens"],
            result["usage"]["output_tokens"],
        )
        self.assertIsInstance(raw, bytes)

    def test_truncated_query_keeps_its_leading_terms(self):
        captured = self.observe("lead", "leadterm appears early")
        trailing = " ".join(f"filler{i}" for i in range(40))
        result = self.run_r(args=self.brief_args(query="leadterm " + trailing))
        self.assertIn(captured["observation"], [i["reference"] for i in result["guidance"]])
        result = self.run_r(args=self.brief_args(query=trailing + " leadterm"))
        self.assertNotIn(captured["observation"], [i["reference"] for i in result["guidance"]])

    def test_newest_matches_are_candidates_even_past_the_window(self):
        for number in range(1, 262):
            self.observe(f"t{number}", f"timeout finding number {number}", observed_at_ms=number)
        newest = self.observe("newest", "timeout finding zebra", observed_at_ms=300)
        result = self.run_r(args=self.brief_args(query="timeout zebra"))
        references = [i["reference"] for i in result["guidance"]]
        self.assertIn(newest["observation"], references)
        self.assertEqual(references[0], newest["observation"])

    def test_optional_excerpt_finds_late_relevance_and_preserves_bounds(self):
        text = "x " * 3000 + "lateuniqueterm café 🔒"
        captured = self.store.capture(
            self.owner,
            self.ns,
            "late",
            {
                "version": 1,
                "project": "p",
                "source": "test",
                "source_event": "late",
                "text": text,
                "readers": None,
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )
        result = self.run_r(args=self.brief_args(query="lateuniqueterm"))
        item = next(i for i in result["guidance"] if i["reference"] == captured["observation"])
        self.assertIn("lateuniqueterm", item["text"])
        self.assertEqual(
            item["text"].encode(),
            text.encode()[item["excerpt_start_byte"] : item["excerpt_end_byte"]],
        )

    def test_capacity_evicts_the_callers_oldest_artifact_and_expiry_erases_content(self):
        clock = self.clock()
        oldest = self.run_r()
        self.store.db.execute("UPDATE rendered_artifacts SET body=zeroblob(134217728)")
        # At capacity the caller's own oldest artifact is evicted; nobody is locked out.
        result = self.run_r()
        self.assertNotEqual(result["receipt"], oldest["receipt"])
        self.fails("replay_expired", lambda: self.receipt(oldest))
        self.assertEqual(
            dict(
                self.store.db.execute(
                    "SELECT state,count(*) FROM rendered_artifacts GROUP BY state"
                )
            ),
            {"evicted": 1, "retained": 1},
        )
        capacity = self.store.status(self.owner, "p")["capacity"]
        self.assertEqual(capacity["rendered_artifacts"]["used"], 1)
        self.assertEqual(capacity["rendered_artifacts"]["limit"], 1024)
        self.assertFalse(capacity["rendered_artifacts"]["warning"])
        clock.advance(RETRY_MS)
        self.store.expire_results()
        self.assertIsNone(
            self.store.db.execute(
                "SELECT body FROM rendered_artifacts WHERE id=?", (result["receipt"],)
            ).fetchone()[0]
        )


if __name__ == "__main__":
    unittest.main()
