"""Content checksums for upload manifests."""

import hashlib


def digest(data):
    return hashlib.sha256(data).hexdigest()


def manifest(files):
    """files: dict of path -> bytes. Returns sorted (path, digest) pairs."""
    return sorted((path, digest(data)) for path, data in files.items())


def verify_manifest(files, text):
    """Sorted paths whose digest is missing or wrong, plus manifest paths with no file."""
    listed = dict(line.split(" ", 1) for line in text.splitlines() if line.strip())
    bad = {path for path, data in files.items() if listed.get(path) != digest(data)}
    bad |= set(listed) - set(files)
    return sorted(bad)
