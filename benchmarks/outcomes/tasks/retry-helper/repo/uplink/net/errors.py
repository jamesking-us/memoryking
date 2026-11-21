class TransportError(Exception):
    """Base class for transport failures."""


class TransientError(TransportError):
    """The request may succeed if it is sent again (timeout, 503)."""


class PermanentError(TransportError):
    """Sending again will not help (401, 404, bad payload)."""
