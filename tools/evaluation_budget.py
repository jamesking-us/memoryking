#!/usr/bin/env python3
"""Illustrative pilot cost worksheet; makes no provider call and makes no power claim."""

import argparse
import json
from decimal import Decimal
from pathlib import Path


def estimate(data):
    def number(key):
        value = Decimal(str(data[key]))
        if not value.is_finite() or value < 0:
            raise ValueError("Inputs must be finite and nonnegative")
        return value

    units = number("tasks") * number("arms") * number("repeats")
    attempts = units * number("attempts_per_task")
    model = (
        attempts
        * (
            number("input_tokens_per_attempt") * number("input_usd_per_million")
            + number("output_tokens_per_attempt") * number("output_usd_per_million")
        )
        / Decimal(1000000)
    )
    review_hours = units * number("review_minutes_per_task_arm") / Decimal(60)
    review = review_hours * number("review_usd_per_hour")
    other = number("shared_processing_usd") + number("compute_usd")

    def money(value):
        return str(value.quantize(Decimal("0.01")))

    return {
        "classification": "estimate_not_authorization",
        "model_usd": money(model),
        "shared_and_compute_usd": money(other),
        "human_review_usd": money(review),
        "total_usd": money(model + other + review),
        "review_hours": money(review_hours),
        "statistical_power": "not_estimated; pilot observations required",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("worksheet", type=Path)
    args = parser.parse_args()
    print(json.dumps(estimate(json.loads(args.worksheet.read_text())), indent=2))
