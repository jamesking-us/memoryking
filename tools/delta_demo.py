"""Real local transport delta and model-reset continuation; synthetic checkpoint only."""

import json
import tempfile
import threading
from pathlib import Path

from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
from memoryking.harness_context import AuthorizedConversation, LocalRecordingProvider
from memoryking.resume_adapter import ResumeAdapter
from memoryking.sdk import LocalClient


def main():
    checkpoint = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures/contracts/checkpoint.json").read_text()
    )
    with tempfile.TemporaryDirectory(prefix="mk-delta-", dir="/tmp") as temp:
        root = Path(temp)
        credential = LocalStore.initialize(root / "data", root / "control")
        with LocalStore(root / "data", root / "control") as store:
            server = Server(store)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                client = LocalClient(server.path, credential, "demo")
                client.working(
                    {
                        "version": 1,
                        "incarnation": client.binding[-1],
                        "action": "save",
                        "key": "save",
                        "args": {
                            "project": "demo",
                            "task": "task",
                            "run": None,
                            "expected_revision": 0,
                            "readers": ["owner"],
                            "checkpoint": checkpoint,
                        },
                    }
                )
                provider = LocalRecordingProvider(root / "provider")
                conversation = AuthorizedConversation(client, provider)
                adapter = ResumeAdapter(client, conversation.model_session_id)
                options = {
                    "task": "task",
                    "bindings": [{}],
                    "max_output_tokens": 6000,
                    "tokenizer": "cl100k_base",
                }
                first = adapter.resume_for(conversation, **options)
                full_usage = adapter.last_envelope["usage"]
                full_transfer = adapter.transfers[0]
                second = adapter.resume_for(conversation, **options)
                delta_usage = adapter.last_envelope["usage"]
                delta_transfer = adapter.transfers[0]
                assert delta_transfer["mode"] == "delta"
                assert first["checkpoint"]["state"] == second["checkpoint"]["state"] == checkpoint
                conversation.add_memory(second)
                conversation.dispatch()
                conversation.reset()
                retained = adapter._target is not None
                resumed = adapter.resume_for(conversation, **options)
                assert adapter.last_envelope["mode"] == "full"
                conversation.add_memory(resumed)
                result = conversation.dispatch()
                actual = json.loads(Path(result["request_path"]).read_bytes())
                assert actual["state_handles"] == [] and len(actual["messages"]) == 1
                assert (
                    json.loads(actual["messages"][0]["content"])["checkpoint"]["state"]
                    == checkpoint
                )
                print(
                    json.dumps(
                        {
                            "passed": True,
                            "scope": "synthetic transport/reset demonstration, not task-quality evidence",
                            "full_transfer": full_transfer,
                            "delta_transfer": delta_transfer,
                            "full_materialized_tokens": full_usage["materialized_tokens"],
                            "delta_materialized_tokens": delta_usage["materialized_tokens"],
                            "retained_adapter_before_reset_resume": retained,
                            "all_essential_fields_in_actual_request": True,
                            "provider_tokens": "unknown",
                            "model_token_savings_claimed": False,
                        }
                    )
                )
            finally:
                server.shutdown()
                thread.join()
                server.server_close()


if __name__ == "__main__":
    main()
