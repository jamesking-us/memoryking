"""Inspect exact local provider requests before and after source purge/context rebuilding."""

import json
import tempfile
import threading
from pathlib import Path

from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
from memoryking.contracts.canonical import canonical_bytes
from memoryking.harness_context import AuthorizedConversation, LocalRecordingProvider
from memoryking.sdk import LocalClient


def main():
    with tempfile.TemporaryDirectory(prefix="mk-infer-", dir="/tmp") as directory:
        root = Path(directory)
        credential = LocalStore.initialize(root / "db", root / "ctl")
        with LocalStore(root / "db", root / "ctl") as store, Server(store) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                client = LocalClient(server.path, credential, "demo")
                restricted = "DISPOSABLE_RESTRICTED_CONTEXT"
                source = client.capture(
                    "demo-source",
                    {
                        "version": 1,
                        "project": "demo",
                        "source": "harness-demo",
                        "source_event": "input1",
                        "text": restricted,
                        "readers": ["owner"],
                        "classification": "restricted",
                        "observed_at_ms": store.clock.utc_ms(),
                    },
                )
                packet = client.retrieval(
                    {
                        "version": 1,
                        "incarnation": store.incarnation,
                        "action": "fetch",
                        "args": {
                            "project": "demo",
                            "kind": "observation",
                            "reference": source["observation"],
                            "start_byte": 0,
                            "end_byte": len(restricted),
                            "max_output_tokens": 2000,
                            "tokenizer": "cl100k_base",
                        },
                    }
                )
                provider = LocalRecordingProvider(root / "provider")
                conversation = AuthorizedConversation(client, provider)
                conversation.add_memory(packet)
                first = conversation.dispatch()
                assert restricted in first["content"]
                store.purge(credential.read_text(), source["observation"])
                conversation.add_host(
                    "Continue from this currently authorized instruction",
                    authorization_id="accepted2",
                    classification="internal",
                    expires_ms=store.clock.utc_ms() + 60000,
                    approved=True,
                )
                second = conversation.dispatch()
                actual = json.loads(Path(second["request_path"]).read_bytes())
                effective = canonical_bytes(provider.effective_messages(actual)).decode()
                assert restricted not in effective
                assert first["handle"] not in canonical_bytes(actual).decode()
                assert actual["state_handles"] == []
                print(
                    json.dumps(
                        {
                            "actual_request_excludes_purged_and_derived_content": restricted
                            not in effective,
                            "old_provider_handles_excluded": actual["state_handles"] == [],
                            "one_use_permit_receipt_recorded": bool(second["permit_receipt"]),
                            "prior_delivery_not_erased": restricted
                            in Path(first["request_path"]).read_text(),
                            "provider": "local-recording",
                            "external_provider_calls": 0,
                        }
                    )
                )
            finally:
                server.shutdown()
                worker.join()


if __name__ == "__main__":
    main()
