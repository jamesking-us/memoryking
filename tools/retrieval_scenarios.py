"""P04 conformance executes real storage/compilation, with no model or fixture replay."""

import hashlib
import json
from unittest.mock import patch

from knowledge_demo import body

from memoryking.adapters.knowledge_store import Knowledge
from memoryking.adapters.local_store import RETRY_MS
from memoryking.adapters.retrieval import Retrieval
from memoryking.domain.local import LocalError


def execute(scenario, operation, arguments):
    s, owner, project = scenario.store, scenario.credential, scenario.project
    number = 0
    raw = arguments.get(
        "corpus_topic", arguments.get("original_bytes", "Evidence for policy review")
    )
    obs = s.capture(
        owner, scenario.namespace, "retrieval-evidence", scenario.body("retrieval-evidence", raw)
    )
    span = {
        "observation": obs["observation"],
        "start_byte": 0,
        "end_byte": len(raw.encode()),
        "content_hash": hashlib.sha256(raw.encode()).hexdigest(),
    }

    def command(action, args, credential=owner, product="retrieval", as_bytes=False):
        nonlocal number
        number += 1
        command = {
            "version": 1,
            "incarnation": s.incarnation,
            "action": action,
            "args": {"project": project, **args},
        }
        if product == "knowledge":
            command["key"] = f"request{number}"
        result = (Knowledge(s) if product == "knowledge" else Retrieval(s)).execute(
            credential, command
        )
        emitted = []
        result.emit(emitted.append)
        return emitted[0] if as_bytes else json.loads(emitted[0])

    def accept(text, key, version=0, constraint=False, **changes):
        claim = body(span, text)
        if constraint:
            claim.update(
                kind="constraint",
                authority="project",
                rule_key=key,
                effect="require",
                rule_value="yes",
            )
        claim.update(changes)
        proposed = command(
            "propose",
            {"logical_key": key, "expected_version": version, "body": claim},
            product="knowledge",
        )
        command(
            "review",
            {
                "revision": proposed["revision"],
                "expected_version": proposed["family_version"],
                "decision": "accept",
                "reason": "fixture_reviewed",
                "breadth_reviewed": True,
            },
            product="knowledge",
        )
        return proposed

    def brief(credential=owner, **changes):
        return command(
            "brief",
            {
                "query": "needle",
                "bindings": [{}],
                "max_output_tokens": 20000,
                "tokenizer": "unknown",
                **changes,
            },
            credential,
        )

    def failure(action, receipt, credential=owner):
        try:
            command(action, {"receipt": receipt}, credential)
            return None
        except LocalError as exc:
            return exc.code

    if operation == "compile_irrelevant_context":
        accept(arguments["corpus_topic"], "unrelated")
        result = brief(query=arguments["query"], max_output_tokens=arguments["budget_tokens"])
        return {
            "optional_memory_count": len(result["guidance"]),
            "status": "abstained" if result["status"] == "no_relevant_memory" else result["status"],
        }
    if operation == "compile_mandatory_overflow":
        accept("Preserve mandatory context. " * 100, "large", constraint=True)
        result = brief(max_output_tokens=arguments["max_output_tokens"])
        return {
            "code": result["status"],
            "silent_truncation": not (
                result["status"] == "budget_insufficient"
                and result["constraints"] == []
                and result["minimum_required_tokens"] > arguments["max_output_tokens"]
            ),
        }
    if operation == "resolve_mandatory_constraints":
        rule = accept("Never expose account credentials", "global", constraint=True)
        result = brief(
            query="astronomy",
            top_k=arguments["top_k"],
            consistency={"required_channels": ["dense"], "on_timeout": "partial"},
        )
        delivered = any(i["reference"] == rule["revision"] for i in result["constraints"])
        with patch("memoryking.adapters.knowledge_store.REGISTRY_LIMIT", 0):
            limited = brief()
        accept(
            "Environment-specific prohibition",
            "environment",
            constraint=True,
            selector={
                "selector_version": "1",
                "any_of": [{"all_of": [{"field": "environment", "op": "eq", "value": "prod"}]}],
            },
        )
        unknown = brief()
        accept(
            "Restricted essential policy",
            "restricted",
            constraint=True,
            readers=["principal_fixture"],
        )
        reader = s.grant(owner, "reader", project, ["read"])
        restricted = brief(reader, bindings=[{"environment": "prod"}])
        accept("Conflicting policy", "other", constraint=True, rule_key="global", rule_value="no")
        conflict = brief(bindings=[{"environment": "prod"}])
        valid = [
            limited["reason"] == "constraint_resolution_incomplete",
            unknown["reason"] == "applicability_unknown",
            restricted["reason"] == "essential_policy_unavailable",
            conflict["reason"] == "unresolved_conflict",
        ]
        return {
            "mandatory_independent_of_rank": delivered,
            "unresolved_reported_sufficient": not all(valid),
        }
    if operation == "repeat_cached_query_after_changes":
        first = brief()
        empty = brief()
        before = command("receipt", {"receipt": empty["receipt"]})
        claim = accept("needle safe migration", "guide")
        added = brief()
        peer = accept("opposite conclusion", "guide", version=2)
        conflict = brief()
        now = s.clock.utc_ms()
        timed = accept("needle temporary", "timed", valid_start=now + 2000, valid_end=now + 6000)
        inactive = brief()
        scenario.clock.offset += 2100
        active = brief()
        scenario.clock.offset += 5000
        ended = brief()
        reader = s.grant(owner, "reader", project, ["read"], expires=s.clock.utc_ms() + 1000)
        old = brief(reader)
        scenario.clock.offset += 1100
        expired = failure("replay", old["receipt"], reader)
        rule = accept("Always preserve the full policy", "mandatory", constraint=True)
        policy = brief()

        def refs(result):
            return {i["reference"] for i in result["guidance"]}

        checks = [
            first["status"] == "no_relevant_memory",
            before["diagnostics"]["cache"] == "hit",
            claim["revision"] in refs(added),
            peer["revision"] in refs(conflict),
            timed["revision"] not in refs(inactive),
            timed["revision"] in refs(active),
            timed["revision"] not in refs(ended),
            expired == "denied",
            rule["revision"] in {i["reference"] for i in policy["constraints"]},
        ]
        return {
            "all_changes_rechecked": all(checks),
            "stale_empty_result_served": claim["revision"] not in refs(added),
        }
    if operation == "replay_then_rerun":
        original = brief(query=raw)
        receipt = original["receipt"]
        args = {"receipt": receipt}
        replay = command("replay", args, as_bytes=True)
        from memoryking.contracts.canonical import canonical_bytes

        same = replay == canonical_bytes(original)
        rerun = command(
            "rerun",
            {
                "predecessor": receipt,
                "brief": {
                    "project": project,
                    "query": arguments["model_rerun_bytes"],
                    "bindings": [{}],
                    "max_output_tokens": 20000,
                    "tokenizer": "unknown",
                },
            },
        )
        missing = failure("replay", "missing")
        reader = s.grant(owner, "reader", project, ["read"])
        readers = brief(reader)
        s.revoke(owner, "reader", project)
        revoked = failure("replay", readers["receipt"], reader)
        scenario.clock.offset += RETRY_MS + 1000
        expired = failure("replay", receipt)
        # Purge follows recorded dependencies, so the packet must have cited the source.
        fresh = brief(query=raw)
        s.purge(owner, obs["observation"])
        purged = failure("replay", fresh["receipt"])
        return {
            "exact_bytes_preserved": same,
            "unavailable_states_distinct": (missing, revoked, expired, purged)
            == ("replay_missing", "denied", "replay_expired", "replay_purged"),
            "rerun_new_receipt": rerun["receipt"] != receipt,
        }
    raise ValueError("unsupported retrieval scenario")
