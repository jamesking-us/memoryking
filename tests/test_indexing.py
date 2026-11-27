"""Indexing speed settings, the batched loop, progress in `status`, and batched vectors.

The vector comparison runs the real model when it is installed (MEMORYKING_MODELS, else the
default models directory) and is skipped otherwise; everything else needs no model."""

import os
import random
import unittest
from unittest.mock import patch

from test_review_cli import DaemonCase

from memoryking import __main__ as cli
from memoryking import semantic
from memoryking.adapters import local_embeddings
from memoryking.adapters.local_embeddings import LocalOnnxEmbedding, default_threads
from memoryking.contracts.canonical import canonical_hash
from memoryking.domain.embeddings import EmbeddingBatch, cosine_ppm


class ThreadSettingTests(unittest.TestCase):
    def threads(self, value, cores=4):
        env = {} if value is None else {"MEMORYKING_EMBEDDING_THREADS": value}
        with (
            patch.dict(os.environ, env),
            patch.object(local_embeddings.os, "cpu_count", return_value=cores),
        ):
            if value is None:
                os.environ.pop("MEMORYKING_EMBEDDING_THREADS", None)
            return default_threads()

    def test_default_leaves_one_core_free_from_one_to_four_threads(self):
        self.assertEqual(self.threads(None, 4), 3)
        self.assertEqual(self.threads(None, 2), 1)
        self.assertEqual(self.threads(None, 1), 1)
        self.assertEqual(self.threads(None, 8), 4)
        self.assertEqual(self.threads(None, 64), 4)

    def test_the_setting_wins_but_never_exceeds_the_cores_and_junk_is_ignored(self):
        self.assertEqual(self.threads("3", 4), 3)
        self.assertEqual(self.threads("64", 4), 4)
        for junk in ("0", "-2", "many", ""):
            self.assertEqual(self.threads(junk, 4), 3, junk)


def model_available():
    return semantic.runtime_problem() is None and semantic.verified()


@unittest.skipUnless(model_available(), "the verified search model is not installed")
class BatchedVectorTests(unittest.TestCase):
    """Texts of mixed length give the same vectors alone, in a batch, and on another thread count."""

    @classmethod
    def setUpClass(cls):
        cls.model = LocalOnnxEmbedding(semantic.model_dir(), semantic.MODEL, threads=2)
        rng = random.Random(3)
        words = ["alpha", "brook", "cedar", "delta", "ember", "fjord", "grove", "harbor"]
        cls.texts = [
            f"turn {n}: " + " ".join(rng.choice(words) for _ in range(size))
            for n, size in enumerate(
                [3, 900, 12, 150, 40, 700, 8, 300, 60, 520, 2, 90, 450, 25, 1, 200]
            )
        ]

    def test_a_mixed_length_batch_equals_one_at_a_time(self):
        alone = [self.model.embed([text]).vectors[0] for text in self.texts]
        together = self.model.embed(self.texts)
        self.assertEqual(len(together.vectors), len(self.texts))
        for one, many in zip(alone, together.vectors, strict=True):
            self.assertLessEqual(max(abs(a - b) for a, b in zip(one, many, strict=True)), 2)
            self.assertGreaterEqual(cosine_ppm(one, many), 999_999)
        # Documented measurement: identical on this model and runtime.
        self.assertEqual(alone, together.vectors)
        self.assertEqual(together.input_hashes, [canonical_hash(text) for text in self.texts])
        self.assertEqual(sum(together.input_token_counts), together.input_tokens)
        self.assertEqual(
            together.truncated, [count >= 512 for count in together.input_token_counts]
        )

    def test_the_order_of_the_texts_does_not_matter(self):
        forward = self.model.embed(self.texts).vectors
        backward = self.model.embed(self.texts[::-1]).vectors[::-1]
        self.assertEqual(forward, backward)

    def test_the_thread_count_does_not_change_a_vector(self):
        single = LocalOnnxEmbedding(semantic.model_dir(), semantic.MODEL, threads=1)
        self.assertEqual(
            single.embed(self.texts[:8]).vectors, self.model.embed(self.texts[:8]).vectors
        )

    def test_a_query_waits_for_at_most_one_group_of_a_document_batch(self):
        import threading
        import time

        many = threading.Thread(target=self.model.embed, args=(self.texts,))
        many.start()
        time.sleep(0.05)
        start = time.perf_counter()
        self.model.embed(["what did we decide?"], kind="query")
        waited = time.perf_counter() - start
        many.join()
        self.assertEqual(self.model.urgent, 0)
        self.assertLess(waited, 5.0)


class FakeBatchModel:
    """A LocalOnnxEmbedding that never loads a model; its vectors are fixed."""

    def __new__(cls):
        provider = object.__new__(LocalOnnxEmbedding)
        provider.fingerprint = semantic.fingerprint_of()
        provider.dimensions = 384
        provider.name = semantic.MODEL
        provider.kinds = True
        provider.calls = []

        def embed(texts, kind="document"):
            provider.calls.append(len(texts))
            return EmbeddingBatch(
                provider.fingerprint,
                384,
                [[1000000] + [0] * 383 for _ in texts],
                [canonical_hash(text) for text in texts],
                3 * len(texts),
                [False] * len(texts),
                [3] * len(texts),
            )

        provider.embed = embed
        return provider


class LoopTests(DaemonCase):
    def test_the_loop_indexes_in_batches_and_status_shows_progress_until_done(self):
        from memoryking.embedding_loop import EmbeddingLoop

        for n in range(10):
            self.capture(f"k{n}", f"Observation number {n} about the release train.")
        provider = FakeBatchModel()
        self.store.embedder = provider
        loop = EmbeddingLoop(
            self.store,
            self.server.path,
            self.owner_file,
            provider,
            batch=4,
            records_per_call=4,
        )
        loop.enable_new_projects()
        first = self.cli("status")
        self.assertIn("p 0 vectors, 0% indexed (10 of 10 records left)", first)
        self.assertNotIn(" a second", first)  # no speed measured yet, so no estimate
        self.assertEqual(loop.tick(), 4)
        self.assertEqual(provider.calls, [4])  # four records, one model call
        loop.tick()
        partial = self.cli("status")
        self.assertIn("records left", partial)
        self.assertIn("a second)", partial)  # speed measured by now, so a time is given
        self.assertRegex(partial, r"about [\w ]+ at [\d.]+ a second")
        while loop.tick():
            pass
        self.assertEqual(sum(provider.calls), 10)
        self.assertLessEqual(max(provider.calls), 4)
        done = self.cli("status")
        self.assertNotIn("records left", done)
        self.assertIn("p 10 vectors", done)
        self.assertEqual(loop.records_per_call, 4)

    def test_records_per_call_stays_between_one_and_sixteen(self):
        from memoryking.embedding_loop import EmbeddingLoop

        for value, expected in ((0, 1), (7, 7), (99, 16)):
            loop = EmbeddingLoop(
                self.store,
                self.server.path,
                self.owner_file,
                FakeBatchModel(),
                records_per_call=value,
            )
            self.assertEqual(loop.records_per_call, expected)

    def test_the_line_is_unchanged_when_nothing_is_left(self):
        line = cli.semantic_line(
            {
                "loaded": True,
                "model": semantic.MODEL,
                "partitions": [
                    {
                        "project": "p",
                        "current_model": True,
                        "state": "experimental",
                        "vectors": 3,
                        "percent_indexed": 100,
                    }
                ],
            }
        )
        self.assertEqual(line, f"{semantic.MODEL}: p 3 vectors")


if __name__ == "__main__":
    unittest.main()
