"""Disposable real local SDK atlas inspection; no model or external service."""

import json
import tempfile
import threading
from pathlib import Path

from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
from memoryking.sdk import LocalClient


def main():
    with tempfile.TemporaryDirectory(prefix="mk-atlas-", dir="/tmp") as temp:
        root = Path(temp)
        credential = LocalStore.initialize(root / "data", root / "control")
        with LocalStore(root / "data", root / "control") as store:
            server = Server(store)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                client = LocalClient(server.path, credential, "demo")
                observation = client.capture(
                    "atlas-demo",
                    {
                        "version": 1,
                        "project": "demo",
                        "source": "example",
                        "source_event": "atlas-demo",
                        "text": "A timeout was observed; cause unverified.",
                        "readers": None,
                        "classification": "internal",
                        "observed_at_ms": 1,
                    },
                )
                result = client.atlas(bindings=[{}], query="timeout", max_nodes=20)
                assert result["nodes"][0]["id"] == observation["observation"]
                assert result["counts"]["observation"] == 1
                assert result["list"][0]["id"] == result["nodes"][0]["id"]
                print(
                    json.dumps(
                        {
                            "authorized_atlas_graph_and_list": True,
                            "count_scope": result["count_scope"],
                            "embedding_model_required": False,
                        }
                    )
                )
            finally:
                server.shutdown()
                thread.join()
                server.server_close()


if __name__ == "__main__":
    main()
