"""A real temporary local store and daemon for the framework adapter tests."""

import importlib.util
import tempfile
import threading
import unittest
from pathlib import Path

from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
from memoryking.sdk import LocalClient


def installed(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


class StoreCase(unittest.IsolatedAsyncioTestCase):
    """Each test gets its own store, daemon and project-bound client."""

    project = "checkout"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-int-")
        root = Path(self.temp.name)
        self.credential = LocalStore.initialize(root / "data", root / "control")
        self.store = LocalStore(root / "data", root / "control")
        self.server = Server(self.store)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.client = LocalClient(self.server.path, self.credential, self.project)

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.store.close()
        self.temp.cleanup()
