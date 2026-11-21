from uplink.net.errors import PermanentError, TransientError
from uplink.net.transport import Transport


class FakeTransport(Transport):
    """Records puts; `failures` maps a path to a list of exceptions raised before success."""

    def __init__(self, failures=None):
        self.stored = {}
        self.calls = []
        self.failures = {k: list(v) for k, v in (failures or {}).items()}

    def put(self, path, data):
        self.calls.append(path)
        queue = self.failures.get(path)
        if queue:
            raise queue.pop(0)
        self.stored[path] = data


__all__ = ["FakeTransport", "PermanentError", "TransientError"]
