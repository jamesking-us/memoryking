#!/usr/bin/env python3
"""Offline fixed-corpus study input/cost audit; never executes a model or claims efficacy.

The supplied frozen protocol hash is an independent input. Content labels, family
assignments and the attempt inventory require independent curation/journal evidence;
this audit cannot discover unlabelled semantic duplicates or omitted activity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from decimal import Decimal
from pathlib import Path

ARMS = ("no_memory", "transcript", "compact_checkpoint", "full_system")
METRICS = ("tokens", "dollars", "cpu_ms", "gpu_ms", "human_minutes", "elapsed_ms")
MAX_BYTES = 16 * 1024 * 1024


def read(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("invalid_or_oversized_input")
    return json.loads(
        path.read_bytes(),
        object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json")),
    )


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(directory, expected_protocol):
    directory = directory.resolve()
    protocol_path = directory / "protocol.json"
    protocol, run = read(protocol_path), read(directory / "run.json")
    errors = []

    def require(condition, reason):
        if not condition:
            errors.append(reason)

    actual = digest(protocol_path)
    require(actual == expected_protocol == run["protocol_sha256"], "protocol_hash_mismatch")
    require(protocol["version"] == 1, "unsupported_protocol")
    require(protocol["cohort"] == "fixed_corpus", "unsupported_cohort")
    require(protocol["arms"] == list(ARMS), "arm_inventory")
    require(protocol["baseline"] in ARMS[:-1], "baseline_not_prespecified")
    require(protocol["allocation"] == "equal_per_arm_once", "unsupported_allocation")
    require(protocol["frozen_at_ms"] < run["started_at_ms"], "protocol_not_frozen_before_run")
    require(protocol["classification"] in {"synthetic", "real_task"}, "classification")
    if protocol["classification"] == "real_task":
        require(protocol["budget_approved"] is True, "real_task_budget_unapproved")
    identities = protocol["identities"]
    for key in (
        "dataset_sha256",
        "source_revision",
        "model_version",
        "tool_version",
        "judge_version",
        "tokenizer_version",
        "resource_profile",
        "stopping_rule",
        "statistical_method",
    ):
        require(isinstance(identities.get(key), str) and bool(identities[key]), "identity:" + key)

    tasks = {row["id"]: row for row in protocol["tasks"]}
    require(bool(tasks) and len(tasks) == len(protocol["tasks"]), "task_inventory")
    split_families, split_duplicates = {}, {}
    for task in tasks.values():
        require(task["split"] in {"development", "holdout"}, "task_split")
        require(task["cutoff_ms"] <= run["started_at_ms"], "task_cutoff")
        require(bool(task["family"]) and bool(task["duplicate_group"]), "curation_missing")
        for key, inventory in (("family", split_families), ("duplicate_group", split_duplicates)):
            previous = inventory.setdefault(task[key], task["split"])
            require(previous == task["split"], "split_overlap:" + key)
    resources = {row["id"]: row for row in protocol["resources"]}
    require(len(resources) == len(protocol["resources"]), "resource_inventory")
    for item in resources.values():
        relative = Path(item["path"])
        target = directory / relative
        require(
            not relative.is_absolute()
            and ".." not in relative.parts
            and not any(
                (directory / Path(*relative.parts[:i])).is_symlink()
                for i in range(1, len(relative.parts) + 1)
            )
            and target.resolve().is_relative_to(directory),
            "resource_path:" + item["id"],
        )
        if errors and errors[-1] == "resource_path:" + item["id"]:
            continue
        require(
            target.is_file() and target.stat().st_size <= MAX_BYTES,
            "resource_missing_or_oversized:" + item["id"],
        )
        if target.is_file() and target.stat().st_size <= MAX_BYTES:
            require(digest(target) == item["sha256"], "resource_hash:" + item["id"])

    evaluated = {key: row for key, row in tasks.items() if row["split"] == run["split"]}
    require(bool(evaluated), "empty_cohort")
    pairs = {(key, arm) for key in evaluated for arm in ARMS}
    memberships = run["initial_memory"]
    require(
        Counter((r["task"], r["arm"]) for r in memberships) == Counter({p: 1 for p in pairs}),
        "memory_pair_inventory",
    )
    namespaces = [r["namespace"] for r in memberships]
    require(all(namespaces) and len(set(namespaces)) == len(namespaces), "cross_arm_namespace")
    for member in memberships:
        task = tasks.get(member["task"])
        if task is None:
            continue
        require(len(member["resources"]) == len(set(member["resources"])), "duplicate_memory_input")
        if member["arm"] == "no_memory":
            require(not member["resources"], "no_memory_seeded")
        for key in member["resources"]:
            item = resources.get(key)
            if item is None:
                errors.append("unknown_resource:" + key)
                continue
            require(item["available_at_ms"] <= task["cutoff_ms"], "future_input:" + key)
            require(item["kind"] == "evidence", "solution_or_postmortem:" + key)
            require(item["sha256"] not in task["excluded_sha256"], "excluded_solution_hash:" + key)
            require(
                item["duplicate_group"] != task["duplicate_group"], "solved_task_duplicate:" + key
            )
            require(
                item["origin_arm"] is None and item["origin_task"] is None,
                "cross_arm_outcome:" + key,
            )

    inventory = run["attempt_inventory"]
    ids = [r["id"] for r in inventory]
    require(bool(ids) and len(ids) == len(set(ids)), "attempt_inventory")
    require({(r["task"], r["arm"]) for r in inventory} == pairs, "attempt_pair_coverage")
    records = run["attempts"]
    require(Counter(r["id"] for r in records) == Counter(ids), "missing_or_duplicate_attempt_cost")
    bindings = {r["id"]: (r["task"], r["arm"]) for r in inventory}
    totals = {arm: {metric: Decimal(0) for metric in METRICS} for arm in ARMS}
    unknown = []

    def add_cost(row, arms):
        require(set(row["cost"]) == set(METRICS), "cost_dimensions:" + row["id"])
        for metric in METRICS:
            value = row["cost"].get(metric)
            if value is None:
                unknown.append({"id": row["id"], "metric": metric, "arms": arms})
                continue
            valid = type(value) in {int, float} and 0 <= value <= 10**18 and math.isfinite(value)
            require(valid, "invalid_cost:" + row["id"] + ":" + metric)
            if valid:
                for arm in arms:
                    totals[arm][metric] += Decimal(str(value)) / len(arms)

    for row in records:
        require(bindings.get(row["id"]) == (row["task"], row["arm"]), "attempt_binding")
        require(row["status"] in {"succeeded", "failed", "unknown"}, "attempt_status")
        if row["arm"] in ARMS:
            add_cost(row, [row["arm"]])
    shared = run["shared_costs"]
    expected_shared = protocol["shared_processing_ids"]
    require(len(expected_shared) == len(set(expected_shared)), "shared_inventory")
    require(
        Counter(r["id"] for r in shared) == Counter(expected_shared),
        "missing_or_duplicate_shared_cost",
    )
    require(not set(expected_shared).intersection(ids), "cost_id_collision")
    for row in shared:
        add_cost(row, list(ARMS))

    results = run["task_results"]
    require(
        Counter((r["task"], r["arm"]) for r in results) == Counter({p: 1 for p in pairs}),
        "task_result_inventory",
    )
    successes = Counter()
    for row in results:
        require(row["outcome"] in {"success", "failure", "unknown"}, "task_outcome")
        if row["outcome"] == "success" and row["arm"] in ARMS:
            successes[row["arm"]] += 1
    return {
        "version": 1,
        "protocol_sha256": actual,
        "run_sha256": digest(directory / "run.json"),
        "classification": protocol["classification"],
        "audit_passed": not errors,
        "errors": sorted(set(errors)),
        "attempts": len(records),
        "attempt_statuses": dict(Counter(r["status"] for r in records)),
        "shared_cost_events": len(shared),
        "known_cost_lower_bounds": {
            a: {k: str(v) for k, v in values.items()} for a, values in totals.items()
        },
        "unknown_cost_dimensions": unknown,
        "successes": dict(successes),
        "tokens_per_success": {
            a: str(totals[a]["tokens"] / successes[a])
            if successes[a]
            and not errors
            and not any(a in r["arms"] and r["metric"] == "tokens" for r in unknown)
            else None
            for a in ARMS
        },
        "complete_cost_accounting": not errors and not unknown,
        "exact_savings_claim_allowed": False,
        "quality_gate": "not_evaluated",
        "efficiency_gate": "not_evaluated",
        "limitations": [
            "Input labels, duplicate groups and journal inventory require independent verification.",
            "This audit checks custody and arithmetic, not semantic leakage detection, statistical efficacy or judge correctness.",
            "Longitudinal cohorts and cold/amortized reuse-horizon comparisons require a separate approved protocol.",
            "Synthetic tests cannot qualify task quality or savings; unknown material cost prevents exact accounting.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--expected-protocol-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Preserve frozen inputs even when the caller chooses a mistaken output path.
    if args.output.resolve().is_relative_to(args.directory.resolve()):
        parser.error("--output must be outside the frozen input directory")
    try:
        result = audit(args.directory, args.expected_protocol_sha256)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        result = {"audit_passed": False, "errors": ["invalid_input:" + type(exc).__name__]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print("Audit passed" if result["audit_passed"] else "Audit failed; see evidence")
    raise SystemExit(0 if result["audit_passed"] else 1)


if __name__ == "__main__":
    main()
