"""Disposable procedure, uncertainty, shadow-ranking and provider-input workflow."""

import hashlib
import json
import tempfile
import threading
from pathlib import Path

from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
from memoryking.domain.local import LocalError
from memoryking.harness_context import AuthorizedConversation, LocalRecordingProvider
from memoryking.sdk import LocalClient


def exercise(client, directory):
    sequence = 0

    def command(action, args, mutation=True):
        nonlocal sequence
        sequence += 1
        return {
            "version": 1,
            "incarnation": client.binding[-1],
            "action": action,
            "args": {"project": client.project, **args},
            **({"key": f"experience-demo-{sequence}"} if mutation else {}),
        }

    def execute(action, args, mutation=True):
        return client.experience(command(action, args, mutation))

    text = "timeout recovery observed in a synthetic fixture"
    observations = [
        client.capture(
            f"experience-{i}",
            {
                "version": 1,
                "project": client.project,
                "source": "synthetic-development",
                "source_event": str(i),
                "text": text,
                "readers": None,
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )["observation"]
        for i in range(2)
    ]
    span = {
        "observation": observations[0],
        "start_byte": 0,
        "end_byte": len(text.encode()),
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
    }
    procedure = {
        "prerequisites": ["An operator checks the current evidence"],
        "steps": ["Inspect the timeout trace before retrying"],
        "verification": ["Confirm the expected result against evidence"],
        "expected_artifacts": ["A verification record"],
        "exceptions": [],
        "failure_signals": ["Missing evidence"],
        "recovery": ["Suspend and reconcile"],
        "known_conditions": ["Synthetic development fixture only"],
        "success_evidence": [observations[0]],
        "failure_evidence": [],
        "failure_assessment": "No observed failure in this fixture; other conditions untested",
        "dependency_mode": "independent",
    }
    proposed = execute(
        "propose",
        {
            "logical_key": "timeout-procedure",
            "expected_version": 0,
            "body": {
                "kind": "procedure",
                "text": "timeout recovery guidance",
                "selector": {"selector_version": "1", "any_of": [{"all_of": [{"op": "any"}]}]},
                "valid_start": None,
                "valid_end": None,
                "readers": None,
                "task": None,
                "run": None,
                "authority": None,
                "effect": None,
                "rule_key": None,
                "rule_value": None,
                "evidence": [span],
                "provenance": {"source": "synthetic-development"},
                "procedure": procedure,
            },
        },
    )
    reviewed = execute(
        "review",
        {
            "revision": proposed["revision"],
            "expected_version": proposed["family_version"],
            "decision": "accept",
            "breadth_reviewed": True,
            "reason": "synthetic_scope_reviewed",
        },
    )
    active = execute(
        "activate",
        {
            "revision": reviewed["revision"],
            "expected_version": reviewed["family_version"],
            "bindings": [{}],
            "policy": "reviewed-procedure-v1",
            "reason": "Explicit_development_activation",
        },
    )
    brief_args = {
        "project": client.project,
        "query": "timeout",
        "bindings": [{}],
        "max_output_tokens": 6000,
        "tokenizer": "cl100k_base",
    }

    def brief(top_k=8):
        return client.retrieval(command("brief", {**brief_args, "top_k": top_k}, False))

    packet = brief()
    assert any(i["authority"] == "active_procedure" for i in packet["guidance"])
    provider = LocalRecordingProvider(directory / "experience-provider")
    host = AuthorizedConversation(client, provider)
    host.add_memory(packet)
    host.dispatch()
    execute(
        "suspend",
        {
            "revision": active["revision"],
            "expected_version": active["family_version"],
            "reason": "Exercise_current-context_invalidation",
        },
    )
    try:
        host.dispatch()
        raise AssertionError("Suspended procedure reached provider")
    except LocalError:
        pass
    fresh = brief()
    host.add_memory(fresh)
    dispatched = host.dispatch()
    actual = json.loads(Path(dispatched["request_path"]).read_bytes())
    assert procedure["steps"][0] not in json.dumps(provider.effective_messages(actual))
    assert len(list(provider.directory.glob("*.json"))) == 2
    outcome = client.tool(
        "memory_feedback",
        command(
            "outcome",
            {
                "logical_key": "demo-run",
                "expected_revision": 0,
                "body": {
                    "task": "demo-task",
                    "run": "demo-run",
                    "result": "unknown",
                    "acceptance_evidence": [],
                    "failures": [],
                    "rework": [],
                    "elapsed_ms": None,
                    "input_tokens": None,
                    "cached_input_tokens": None,
                    "output_tokens": None,
                    "limitations": ["No real task or language model evaluated"],
                    "readers": None,
                },
            },
        ),
    )
    latest = execute("record", {"kind": "outcome", "record": outcome["record"]}, False)
    assert latest["body"]["result"] == "unknown" and latest["body"]["input_tokens"] is None
    packet = brief(16)
    target = packet["guidance"][-1]["reference"]
    vote = client.tool(
        "memory_feedback",
        command(
            "feedback",
            {
                "receipt": packet["receipt"],
                "reference": target,
                "task": "demo-task",
                "run": "demo-run",
                "expected_revision": 0,
                "use": "used",
                "usefulness": "helpful",
                "comment": "Synthetic target",
                "readers": None,
            },
        ),
    )
    configuration = execute(
        "ranking_propose",
        {
            "logical_key": "demo-ranking",
            "feedback": [vote["record"]],
            "max_adjustment_ppm": 100000,
            "policy": "bounded-feedback-v1",
        },
    )
    shadow = execute(
        "ranking_evaluate",
        {
            "configuration": configuration["record"],
            "logical_key": "demo-shadow",
            "cases": [{"brief": {**brief_args, "top_k": 1}, "relevant": [target], "forbidden": []}],
            "dataset_kind": "development",
            "judgment_provenance": "Synthetic target, not held-out quality evidence",
        },
    )
    evaluation = execute("record", {"kind": "shadow", "record": shadow["record"]}, False)["body"]
    assert evaluation["status"] == "passes_development_gates"
    execute(
        "ranking_activate",
        {
            "configuration": configuration["record"],
            "shadow": shadow["record"],
            "expected_version": 0,
            "reason": "Reviewed_synthetic_improvement",
        },
    )
    changed = brief(1)
    assert changed["guidance"][0]["reference"] == target
    execute(
        "ranking_rollback",
        {"configuration": None, "expected_version": 1, "reason": "Finish_at_baseline"},
    )
    replay = client.retrieval(command("replay", {"receipt": changed["receipt"]}, False))
    assert replay == changed and brief(1)["guidance"][0]["reference"] != target
    return {
        "passed": True,
        "scope": "synthetic development; local recording provider only",
        "procedure_review_activation_suspension": True,
        "actual_provider_input_excludes_suspended_steps": True,
        "unknown_outcome_and_usage_preserved": True,
        "feedback_assurance": "retrieved_verified_usefulness_actor_reported",
        "shadow": evaluation["cases"][0]["arms"],
        "ranking_rolled_back": True,
        "exact_replay_preserved": True,
        "task_quality_qualified": False,
        "paid_provider_calls": 0,
    }


def main():
    with tempfile.TemporaryDirectory(prefix="mk-experience-", dir="/tmp") as temp:
        root = Path(temp)
        credential = LocalStore.initialize(root / "data", root / "control")
        with LocalStore(root / "data", root / "control") as store:
            server = Server(store)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                print(
                    json.dumps(
                        exercise(LocalClient(server.path, credential, "demo"), root), indent=2
                    )
                )
            finally:
                server.shutdown()
                thread.join()
                server.server_close()


if __name__ == "__main__":
    main()
