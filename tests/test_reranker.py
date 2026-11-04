"""The optional local reranker: pinned files, bounded inputs, a stage that only reorders."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_experience as experience
import test_knowledge as knowledge
import test_postgres_parity as pg

from memoryking.adapters.local_embeddings import (
    RERANK_PAIRS,
    RERANKER,
    LocalOnnxReranker,
    model_files,
    model_manifest,
)
from memoryking.domain.embeddings import fingerprint
from memoryking.domain.local import LocalError

INSTALLED = os.environ.get("MEMORYKING_RERANKER_MODEL")


class RerankerModelTests(unittest.TestCase):
    def test_manifest_pins_a_public_cross_encoder_with_hashes_and_runtime(self):
        manifest = model_manifest(RERANKER)
        self.assertEqual(manifest["schema"], "reranker-model-v1")
        self.assertEqual(manifest["kind"], "cross-encoder")
        self.assertEqual(manifest["license"], "Apache-2.0")
        self.assertFalse(manifest["remote_code"])
        self.assertEqual(manifest["activation"], "off-unless-requested")
        self.assertEqual(
            {f["path"] for f in manifest["files"]} >= {"onnx/model.onnx", "tokenizer.json"}, True
        )
        for file in manifest["files"]:
            self.assertTrue(file["url"].startswith("https://huggingface.co/cross-encoder/"))
            self.assertIn(manifest["revision"], file["url"])
            self.assertEqual(len(file["sha256"]), 64)
        self.assertEqual(manifest["runtime"], model_manifest()["runtime"])
        self.assertNotEqual(fingerprint(manifest), fingerprint(model_manifest()))

    def test_absent_or_altered_files_are_refused_without_any_download(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
                with self.assertRaisesRegex(LocalError, "reranker_model_not_installed"):
                    LocalOnnxReranker(root)
            manifest = model_manifest(RERANKER)
            for file in manifest["files"]:
                path = root / file["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x" * file["bytes"])
            with self.assertRaisesRegex(LocalError, "reranker_model_checksum_mismatch"):
                model_files(root, manifest)
            with self.assertRaisesRegex(LocalError, "reranker_model_checksum_mismatch"):
                LocalOnnxReranker(root)

    @unittest.skipUnless(INSTALLED, "MEMORYKING_RERANKER_MODEL names no installed reranker")
    def test_installed_model_scores_relevance_deterministically_within_limits(self):
        reranker = LocalOnnxReranker(Path(INSTALLED))
        self.assertEqual(reranker.fingerprint, fingerprint(model_manifest(RERANKER)))
        query = "How many retries does the payment service make?"
        texts = [
            "Lunch was good and the weather held.",
            "The payment service retries a failed charge three times with backoff.",
        ]
        first = reranker.score(query, texts)
        self.assertEqual(first, reranker.score(query, texts))
        self.assertGreater(first[1], first[0])
        self.assertTrue(all(0 <= s <= 1_000_000 for s in first))
        # A text longer than the window is cut, the query never is, and the call still scores.
        long = reranker.score(query, [texts[1] + " filler" * 2000])
        self.assertEqual(len(long), 1)
        for bad in ([], ["x"] * (RERANK_PAIRS + 1), [""], ["y" * 16385]):
            with self.assertRaisesRegex(LocalError, "reranker_input_limit"):
                reranker.score(query, bad)
        with self.assertRaisesRegex(LocalError, "reranker_input_limit"):
            reranker.score("", texts)


class FakeReranker:
    """Prefers texts carrying `favourite`; records every call."""

    def __init__(self, favourite="alpha", fail=False):
        self.favourite, self.fail, self.calls = favourite, fail, []

    fingerprint = "fake-reranker-fingerprint"

    def score(self, query, texts):
        self.calls.append((query, list(texts)))
        if self.fail:
            raise LocalError("reranker_failure")
        return [900_000 if self.favourite in text else 100_000 for text in texts]


class RerankCases:
    run_r = experience.ProcedureCases.run_r
    brief_args = experience.ProcedureCases.brief_args

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

    def stage(self, result, name="rerank_v1"):
        diag = self.run_r("receipt", {"project": "p", "receipt": result["receipt"]})["diagnostics"]
        return next(s for s in diag["trace"] if s["stage"] == name), diag

    def order(self, result):
        return [item["reference"] for item in result["guidance"]]

    def test_rerank_reorders_only_the_fused_window_and_only_when_asked(self):
        alpha = self.capture("alpha", "timeout alpha: the first note about the timeout.")
        beta = self.capture("beta", "timeout beta: a later note about the timeout.")
        gamma = self.capture("gamma", "timeout gamma: the latest note about the timeout.")
        plain = self.run_r()
        default_order = self.order(plain)
        self.assertEqual(set(default_order) >= {alpha, beta, gamma}, True)
        stage, diag = self.stage(plain)
        self.assertEqual(stage, {"stage": "rerank_v1", "state": "off_unless_requested"})
        self.assertNotIn("rerank_score_ppm", plain["guidance"][0])
        # Asking without a model is honest, not an error, and changes nothing.
        no_model = self.run_r(args=self.brief_args(rerank="on"))
        self.assertEqual(self.order(no_model), default_order)
        self.assertEqual(self.stage(no_model)[0]["state"], "unavailable_no_model")
        # Equal lexical scores use random observation IDs as a stable tie-break.
        # Prefer the last captured item in the actual baseline, so this case
        # exercises a real reorder even when alpha happens to start first.
        favourite = next(ref for ref in reversed(default_order) if ref in {alpha, beta, gamma})
        word = next(
            word
            for word, ref in (("alpha", alpha), ("beta", beta), ("gamma", gamma))
            if ref == favourite
        )
        self.store.reranker = FakeReranker(word)
        self.assertEqual(self.order(self.run_r()), default_order)  # still off by default
        on = self.run_r(args=self.brief_args(rerank="on"))
        self.assertEqual(self.order(on)[0], favourite)
        self.assertEqual(self.order(on)[1:], [r for r in default_order if r != favourite])
        scores = {i["reference"]: i["rerank_score_ppm"] for i in on["guidance"]}
        self.assertEqual(scores[favourite], 900_000)
        self.assertTrue(all(score == 100_000 for ref, score in scores.items() if ref != favourite))
        stage, diag = self.stage(on)
        self.assertEqual(stage["state"], "applied")
        self.assertEqual(stage["model"], "fake-reranker-fingerprint")
        self.assertEqual(stage["candidates"], len(default_order))
        self.assertGreaterEqual(stage["moved"], 2)
        self.assertEqual(stage["window"], 16)
        self.assertIn("fake-reranker-fingerprint", diag["model_fingerprints"])
        names = [s["stage"] for s in diag["trace"]]
        self.assertLess(names.index("salience_v1"), names.index("rerank_v1"))
        self.assertLess(names.index("rerank_v1"), names.index("rank"))
        query, texts = self.store.reranker.calls[-1]
        self.assertEqual(query, "timeout")
        self.assertEqual(len(texts), len(default_order))
        # The receipt replays the reranked packet byte for byte.
        replay = self.run_r("replay", {"project": "p", "receipt": on["receipt"]})
        self.assertEqual(self.order(replay), self.order(on))
        # A model failure keeps the fused order and says so.
        self.store.reranker = FakeReranker(fail=True)
        failed = self.run_r(args=self.brief_args(rerank="on"))
        self.assertEqual(self.order(failed), default_order)
        self.assertEqual(
            self.stage(failed)[0],
            {"stage": "rerank_v1", "state": "failed_fused_order_kept", "error": "reranker_failure"},
        )
        self.assertNotIn("rerank_score_ppm", failed["guidance"][0])
        # The model never admits a record: an unrelated note stays out however it scores.
        self.store.reranker = FakeReranker("garden")
        self.capture("garden", "The garden gate squeaks.")
        unrelated = self.run_r(args=self.brief_args(rerank="on"))
        self.assertNotIn("garden", " ".join(i["text"] for i in unrelated["guidance"]))

    def test_rerank_is_allowed_for_as_of_views_and_refused_by_the_contract_when_malformed(self):
        self.store.reranker = FakeReranker("timeout=30")
        historical = self.run_r(args=self.brief_args(rerank="on", as_of_ms=4102444800000))
        self.assertEqual(self.stage(historical)[0]["state"], "applied")
        with self.assertRaisesRegex(LocalError, "invalid_request"):
            self.run_r(args=self.brief_args(rerank=True))


class LocalRerankTests(RerankCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown


class PostgresRerankTests(RerankCases, pg.NativeFixture):
    pass
