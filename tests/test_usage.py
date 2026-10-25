"""Accounting arithmetic fixtures, not product/task conformance evidence."""

import unittest
from dataclasses import replace

from memoryking.domain.local import LocalError
from memoryking.domain.usage import UsageRecord, summarize


class UsageTests(unittest.TestCase):
    def record(self, **changes):
        return UsageRecord(
            **{
                "attempt": "one",
                "provider": "fixture",
                "fingerprint": "model-v1",
                "stage": "context_input",
                "status": "succeeded",
                "input_tokens": 100,
                "cached_input_tokens": 40,
                "output_tokens": 20,
                "cost_microusd": 30,
                "duration_ms": 10,
                **changes,
            }
        )

    def test_failures_retries_background_and_cache_are_counted_once(self):
        records = [
            self.record(),
            self.record(attempt="retry", stage="retry", status="failed"),
            self.record(
                attempt="background",
                stage="embedding_backfill",
                provider="local",
                fingerprint="different",
                input_tokens=30,
                cached_input_tokens=0,
                output_tokens=0,
                cost_microusd=0,
            ),
        ]
        result = summarize(records, 1)
        self.assertEqual(result["total_tokens"], 270)
        self.assertEqual(result["input_tokens"]["total"], 230)
        self.assertEqual(result["cached_input_tokens"]["total"], 80)
        self.assertEqual(result["uncached_input_tokens"]["total"], 150)
        self.assertEqual(result["cost_microusd"]["total"], 60)
        self.assertEqual(result["statuses"]["failed"], 1)
        self.assertEqual(len(result["providers"]), 2)
        self.assertEqual(result["tokens_per_successful_task"]["numerator"], 270)

    def test_unknown_provider_usage_and_cost_never_become_zero(self):
        result = summarize(
            [
                self.record(),
                self.record(
                    attempt="lost",
                    status="unknown",
                    input_tokens=None,
                    cached_input_tokens=None,
                    output_tokens=None,
                    cost_microusd=None,
                ),
            ],
            2,
        )
        self.assertIsNone(result["total_tokens"])
        self.assertEqual(result["known_token_lower_bound"], 120)
        self.assertIsNone(result["cost_microusd"]["total"])
        self.assertEqual(result["tokens_per_successful_task"]["status"], "unknown_usage")
        self.assertEqual(result["tokens_per_successful_task"]["denominator"], 2)

    def test_no_success_is_undefined_and_bad_or_duplicate_usage_rejects(self):
        self.assertEqual(
            summarize([self.record(status="failed")], 0)["tokens_per_successful_task"]["status"],
            "undefined_no_success",
        )
        for change in [
            {"input_tokens": -1},
            {"cached_input_tokens": 101},
            {"duration_ms": True},
            {"output_tokens": 2.5},
            {"stage": "invented"},
        ]:
            with self.assertRaises(LocalError):
                replace(self.record(), **change).validate()
        with self.assertRaises(LocalError):
            summarize([self.record(), self.record()], 1)
