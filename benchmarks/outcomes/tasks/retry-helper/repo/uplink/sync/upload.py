"""Push local files to the remote store."""

from uplink.sync.checksum import manifest


def upload_file(transport, path, data):
    transport.put(path, data)
    return path


def upload_all(transport, files):
    """Upload every file (dict of path -> bytes), then the manifest. Returns the paths sent."""
    sent = [upload_file(transport, path, data) for path, data in sorted(files.items())]
    lines = "\n".join(f"{path} {digest}" for path, digest in manifest(files))
    transport.put("MANIFEST", lines.encode())
    return sent
