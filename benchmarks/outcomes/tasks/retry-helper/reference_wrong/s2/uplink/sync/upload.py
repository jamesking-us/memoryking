"""Push local files to the remote store."""

from uplink.net.backoff import backoff
from uplink.sync.checksum import manifest


def upload_file(transport, path, data):
    @backoff(max_tries=4, base=0.05)
    def send():
        transport.put(path, data)

    send()
    return path


def upload_all(transport, files):
    """Upload every file (dict of path -> bytes), then the manifest. Returns the paths sent."""
    sent = [upload_file(transport, path, data) for path, data in sorted(files.items())]
    lines = "\n".join(f"{path} {digest}" for path, digest in manifest(files))
    upload_file(transport, "MANIFEST", lines.encode())
    return sent
