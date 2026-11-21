"""Push local files to the remote store."""

from uplink.sync.checksum import manifest
from uplink.util.retry import call_with_retry

ATTEMPTS = 4


def upload_file(transport, path, data):
    call_with_retry(lambda: transport.put(path, data), "upload", attempts=ATTEMPTS)
    return path


def upload_all(transport, files):
    """Upload every file (dict of path -> bytes), then the manifest. Returns the paths sent."""
    sent = [upload_file(transport, path, data) for path, data in sorted(files.items())]
    lines = "\n".join(f"{path} {digest}" for path, digest in manifest(files))
    upload_file(transport, "MANIFEST", lines.encode())
    return sent
