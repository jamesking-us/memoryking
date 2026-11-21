"""The wire interface the sync code talks to. Real implementations live elsewhere."""

from uplink.net.errors import PermanentError


class Transport:
    def put(self, path, data):
        """Store data (bytes) at path. May raise TransientError or PermanentError."""
        raise PermanentError("no transport configured")

    def get(self, path):
        raise PermanentError("no transport configured")
