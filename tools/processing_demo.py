"""Actual SDK/compiler/provider-byte demonstration using synthetic source text."""

import json
import tempfile
import threading
from pathlib import Path

from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
from memoryking.contracts.canonical import canonical_hash
from memoryking.domain.compression import critical
from memoryking.harness_context import AuthorizedConversation, LocalRecordingProvider
from memoryking.sdk import LocalClient


def exercise(client, directory):
    checkpoint = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures/contracts/checkpoint.json").read_text()
    )
    checkpoint["objective"] = "Do not delete production records."
    checkpoint["current_state"] = "Verification failed; reconcile the ambiguous operation."

    def command(action, args):
        return {"version": 1, "incarnation": client.binding[-1], "action": action, "args": args}

    state = command(
        "save",
        {
            "project": client.project,
            "task": "task",
            "run": None,
            "expected_revision": 0,
            "readers": ["owner"],
            "checkpoint": checkpoint,
        },
    )
    state["key"] = "save"
    client.working(state)
    text = "Do NOT retry the mutation: the first attempt may have succeeded. Verification failed."
    observations = [
        client.capture(
            str(i),
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
    args = {
        "project": client.project,
        "task": "task",
        "run": None,
        "checkpoint": None,
        "bindings": [{}],
        "max_output_tokens": 6000,
        "tokenizer": "cl100k_base",
        "sources": observations,
        "claims": [],
        "candidate": None,
    }
    first = client.processing(command("compile", args))
    explanation = first["optional_explanation"]
    assert len(explanation["quotes"]) == 1 and len(explanation["quotes"][0]["observations"]) == 2
    unsafe = [{"observations": observations, "text": "Retry immediately. Verification passed."}]
    candidate = {
        "critical_hash": explanation["critical_hash"],
        "quotes": unsafe,
        "attempt": "synthetic-attempt",
        "provenance": {
            "provider": "development-fixture",
            "model": "no-model",
            "configuration_hash": canonical_hash({"fixture": 1}),
            "prompt_revision": "p08",
            "input_hash": explanation["source_hash"],
            "output_hash": canonical_hash(unsafe),
        },
    }
    packet = client.processing(command("compile", {**args, "candidate": candidate}))
    assert packet["optional_explanation"]["reason"] == "semantic_equivalence_unproven"
    assert critical(packet) == critical(first)
    provider = LocalRecordingProvider(directory / "provider")
    host = AuthorizedConversation(client, provider)
    host.add_memory(packet)
    dispatched = host.dispatch()
    actual = json.loads(Path(dispatched["request_path"]).read_bytes())
    memory = json.loads(actual["messages"][0]["content"])
    assert memory["checkpoint"]["state"] == checkpoint
    assert memory["optional_explanation"]["quotes"][0]["text"] == text
    assert unsafe[0]["text"] not in json.dumps(actual)
    usage = client.processing(command("usage", {"project": client.project, "successful_tasks": 0}))
    assert usage["unknown_token_attempts"] == 1 and usage["total_tokens"] is None
    return {
        "passed": True,
        "scope": "synthetic SDK/provider-request check, not model-quality evidence",
        "sources": 2,
        "unique_quotes": 1,
        "fallback_reason": packet["optional_explanation"]["reason"],
        "critical_state_verbatim": True,
        "actual_provider_request_checked": True,
        "processing_usage": usage,
        "model_token_savings": "not_evaluated",
    }


def main():
    with tempfile.TemporaryDirectory(prefix="mk-processing-", dir="/tmp") as temp:
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
