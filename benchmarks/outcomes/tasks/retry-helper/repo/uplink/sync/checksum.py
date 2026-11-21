"""Content checksums for upload manifests."""

import hashlib


def digest(data):
    return hashlib.sha256(data).hexdigest()


def manifest(files):
    """files: dict of path -> bytes. Returns sorted (path, digest) pairs."""
    return sorted((path, digest(data)) for path, data in files.items())
