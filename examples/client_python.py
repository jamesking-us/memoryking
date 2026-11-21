"""Use the Python client: remember, recall, save progress, resume, hand off, in sync and async code."""

import asyncio
import tempfile
import threading
from pathlib import Path

from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
from memoryking.client import MemoryKing


def main():
    with tempfile.TemporaryDirectory(prefix="mk-client-", dir="/tmp") as directory:
        root = Path(directory)
        credential = LocalStore.initialize(root / "data", root / "control")
        with LocalStore(root / "data", root / "control") as store, Server(store) as server:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            # With MEMORYKING_PROFILE, or MEMORYKING_SOCKET, MEMORYKING_CREDENTIAL and
            # MEMORYKING_PROJECT set, `MemoryKing.connect()` needs no arguments.
            where = {"socket": server.path, "credential": credential, "project": "checkout"}
            with MemoryKing.connect(**where) as memory:
                memory.remember("Raising every timeout exceeded the total retry deadline.")
                briefing = memory.recall("timeouts")
                print(briefing.status, "-", len(briefing.sources), "cited source")
                memory.save_progress(
                    "retry-fix",
                    "Fix the retry deadline",
                    "Investigating",
                    next_actions=["Run the regression"],
                    share_with=["reviewer"],
                )
                print(memory.resume("retry-fix").text.splitlines()[0])
                store.grant(credential.read_text().strip(), "reviewer", "checkout", ["read"])
                print("handoff", memory.hand_off("retry-fix", "reviewer").status)

            async def later():
                # The same surface for asyncio; calls run on worker threads.
                async with await MemoryKing.aconnect(**where) as memory:
                    found = await memory.recall("timeouts")
                    return found.sources[0]["ref"]

            print("async recall cites", asyncio.run(later()))
        print("Client example passed.")


if __name__ == "__main__":
    main()
