"""A throwaway local store and daemon so the framework examples run with no setup.

A real program connects to its own store instead: `MemoryKing.connect()` reads
MEMORYKING_PROFILE (or MEMORYKING_SOCKET, MEMORYKING_CREDENTIAL and MEMORYKING_PROJECT).
"""

import contextlib
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
from memoryking.sdk import LocalClient


@contextlib.contextmanager
def demo_store(project: str = "checkout") -> Iterator[tuple[LocalClient, Server, Path]]:
    """Yield a client bound to `project`, the daemon and the owner credential file."""
    with tempfile.TemporaryDirectory(prefix="mk-demo-", dir="/tmp") as directory:
        root = Path(directory)
        credential = LocalStore.initialize(root / "data", root / "control")
        with LocalStore(root / "data", root / "control") as store, Server(store) as server:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            yield LocalClient(server.path, credential, project), server, credential
            server.shutdown()
