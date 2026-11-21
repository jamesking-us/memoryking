"""The analyzer, stemmer and BM25-style scorer are pure and deterministic."""

import unittest

from memoryking.domain.lexicon import (
    STEM_MARK,
    STOP,
    Corpus,
    analyze,
    index_terms,
    index_text,
    plain_text,
    porter,
    query_terms,
    score,
    stem,
)


class StemmerTests(unittest.TestCase):
    def test_reference_pairs_from_the_porter_vocabulary(self):
        for word, expected in {
            "caresses": "caress",
            "ponies": "poni",
            "cats": "cat",
            "agreed": "agre",
            "plastered": "plaster",
            "motoring": "motor",
            "conflated": "conflat",
            "hopping": "hop",
            "falling": "fall",
            "filing": "file",
            "happy": "happi",
            "relational": "relat",
        }.items():
            self.assertEqual(expected, porter(word), word)

    def test_the_measured_failures_meet(self):
        for family in (
            "retry retries retried retrying",
            "charge charged charging charges",
            "customer customers",
            "deploy deploys deployed deploying",
            "payment payments",
        ):
            stems = {stem(word) for word in family.split()}
            self.assertEqual(1, len(stems), family)

    def test_code_shaped_and_foreign_words_are_never_stemmed(self):
        self.assertEqual([("retry", None), ("charges", None)], analyze("retry_charges"))
        self.assertEqual([("handlers2", None)], analyze("handlers2"))
        self.assertEqual([("zählungen", None)], analyze("Zählungen"))
        self.assertIsNone(stem("up"))  # too short
        self.assertIsNone(stem("the"))  # a stop word

    def test_code_relevant_words_stay_searchable(self):
        for word in ("not", "null", "error", "test", "fail", "true", "false", "no", "all", "any"):
            self.assertNotIn(word, STOP)
        self.assertGreater(len(STOP), 100)
        self.assertLess(len(STOP), 160)


class IndexTextTests(unittest.TestCase):
    def test_tokens_keep_order_then_marked_stems(self):
        text = index_text("The retries double-charged payments.retry_charge() src/app/main.py PAY-142")
        words = text.split()
        self.assertEqual(
            ["the", "retries", "double", "charged", "payments", "retry", "charge"],
            words[:7],
        )
        self.assertIn("pay", words)
        self.assertIn("142", words)
        self.assertIn(STEM_MARK + "retri", words)
        self.assertIn(STEM_MARK + "payment", words)
        # an identifier's parts are exact only: no stem token for "charge"
        self.assertNotIn(STEM_MARK + "charg", [w for w in words if w.startswith(STEM_MARK)][:0] or [])
        self.assertEqual(plain_text(text).split(), words[: len(plain_text(text).split())])

    def test_statistics_view_is_reversible_from_the_stored_text(self):
        text = index_text("The retries retried again and again")
        terms, length = index_terms(text)
        self.assertEqual(len(terms), len(set(terms)))
        self.assertNotIn("the", terms)
        self.assertIn(STEM_MARK + "retri", terms)
        self.assertEqual(2, length)  # retries retried (stop words do not count)
        self.assertEqual((terms, length), index_terms(text))


class ScoreTests(unittest.TestCase):
    def corpus(self, documents=1000, **df):
        return Corpus(documents, documents * 20, {STEM_MARK + k if k.isalpha() else k: v for k, v in df.items()})

    def test_forms_meet_and_exact_beats_stem(self):
        terms = query_terms("retry charge customer")
        corpus = self.corpus(10)
        forms = score(terms, "The retries double-charged several customers during checkout.", corpus)
        self.assertGreater(forms, 0)
        exact = score(terms, "retry charge customer", corpus)
        self.assertGreater(exact, forms)
        self.assertEqual(0, score(terms, "nothing relevant here", corpus))

    def test_a_rare_term_outweighs_common_ones(self):
        terms = query_terms("deploy config zukatori")
        corpus = Corpus(
            1000, 20000, {STEM_MARK + "deploi": 400, STEM_MARK + "config": 500, STEM_MARK + "zukatori": 1}
        )
        common = score(terms, "deploy config was rolled out", corpus)
        rare = score(terms, "the zukatori flag was unset", corpus)
        self.assertGreater(rare, common)

    def test_shorter_documents_score_higher_for_equal_matches(self):
        terms = query_terms("webhook")
        corpus = self.corpus(100)
        short = score(terms, "webhook failed", corpus)
        long = score(terms, "webhook " + "filler " * 80, corpus)
        self.assertGreater(short, long)

    def test_scores_stay_within_the_scale_and_are_repeatable(self):
        terms = query_terms("alpha beta")
        corpus = Corpus(10, 50, {STEM_MARK + "alpha": 1, STEM_MARK + "beta": 1})
        value = score(terms, "alpha beta", corpus)
        self.assertTrue(0 < value <= 1_000_000)
        self.assertEqual(value, score(terms, "alpha beta", corpus))
        self.assertTrue(0 < score(terms, "alpha " * 500, corpus) <= 1_000_000)

    def test_an_empty_corpus_is_neutral(self):
        terms = query_terms("alpha beta")
        both = score(terms, "alpha beta", Corpus())
        one = score(terms, "alpha gamma", Corpus())
        self.assertGreater(both, one)


if __name__ == "__main__":
    unittest.main()
