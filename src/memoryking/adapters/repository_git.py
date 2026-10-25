"""Trusted local Git data adapter: no checkout, hooks, filters, builds or fetching."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from memoryking.contracts.canonical import canonical_hash
from memoryking.domain.local import LocalError
from memoryking.domain.repository import MAX_FILE_BYTES, PARSER, project_source
from memoryking.domain.selectors import path_valid

MAX_FILES = 2048
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_GIT_BYTES = 4 * 1024 * 1024
MAX_ANCESTORS = 512


def canonical_remote(value: str) -> str:
    if value.startswith("local:") and re.fullmatch(r"local:[A-Za-z0-9_.-]{1,96}", value):
        return value
    if re.fullmatch(r"[^/@:]+@[^/:]+:.+", value):
        user, rest = value.split("@", 1)
        host, path = rest.split(":", 1)
        value = f"ssh://{user}@{host}/{path}"
    parts = urlsplit(value)
    if (
        parts.scheme not in {"https", "ssh"}
        or not parts.hostname
        or parts.query
        or parts.fragment
        or parts.password
    ):
        raise LocalError("repository_identity_invalid")
    if parts.scheme == "https" and parts.username:
        raise LocalError("repository_identity_invalid")
    path = parts.path.strip("/").removesuffix(".git")
    if not path_valid(path) or any(c.isspace() for c in path) or "%" in path:
        raise LocalError("repository_identity_invalid")
    # Normalize the conventional hosted Git account; preserve custom SSH users
    # because their relative repository roots can identify different content.
    # Explicit admin registration is an identity assertion, not remote discovery.
    port = parts.port
    authority = parts.hostname.lower()
    if port is not None and port not in ({22} if parts.scheme == "ssh" else {443}):
        authority += f":{port}"
    if parts.scheme == "ssh" and parts.username != "git":
        user = parts.username or "default-user"
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", user):
            raise LocalError("repository_identity_invalid")
        authority = f"ssh-{user}@{authority}"
    return f"remote:{authority}/{path}"


class GitSnapshot:
    def __init__(self, root: Path):
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise LocalError("repository_path_invalid")
        self.root = root.resolve(strict=True)
        git = shutil.which("git")
        if git is None:
            raise LocalError("git_unavailable")
        self.git = git
        self.deadline = time.monotonic() + 12
        top = self.run("rev-parse", "--show-toplevel").decode().strip()
        if Path(top).resolve() != self.root:
            raise LocalError("repository_root_required")

    def run(self, *args: str) -> bytes:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise LocalError("repository_capture_limit")
        env = {
            "PATH": os.defpath,
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }
        command = [
            self.git,
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.untrackedCache=false",
            "-c",
            "protocol.allow=never",
            "-c",
            "core.pager=cat",
            "-C",
            str(self.root),
            *args,
        ]
        # Spool to private temporary files, bounding memory and process duration.
        # Poll size so hostile repository metadata cannot grow output without bound.
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env
            )
            try:
                while proc.poll() is None:
                    if (
                        time.monotonic() >= self.deadline
                        or os.fstat(out.fileno()).st_size > MAX_GIT_BYTES
                        or os.fstat(err.fileno()).st_size > MAX_GIT_BYTES
                    ):
                        raise LocalError("repository_capture_limit")
                    time.sleep(0.005)
                if proc.returncode or os.fstat(out.fileno()).st_size > MAX_GIT_BYTES:
                    raise LocalError("repository_data_unavailable")
                out.seek(0)
                return out.read(MAX_GIT_BYTES + 1)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()

    def _paths(self) -> list[str]:
        raw = self.run("ls-files", "--cached", "--others", "--exclude-standard", "-z")
        try:
            paths = sorted(set(p.decode("utf-8") for p in raw.split(b"\0") if p))
        except UnicodeError as exc:
            raise LocalError("repository_paths_unsupported") from exc
        if len(paths) > MAX_FILES or any(
            not path_valid(p) or ".git" in p.split("/") for p in paths
        ):
            raise LocalError("repository_capture_limit")
        return paths

    def _read(self, path: str) -> tuple[bytes | None, str]:
        if time.monotonic() >= self.deadline:
            raise LocalError("repository_capture_limit")
        # Walk using directory descriptors and NOFOLLOW at every component. Never
        # traverse submodules, symlinks, devices or a path swapped outside the root.
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parts = path.split("/")
            for part in parts[:-1]:
                new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = new
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                before = os.fstat(file_fd)
                if not stat.S_ISREG(before.st_mode):
                    return None, "unsupported_file_type"
                if before.st_size > MAX_FILE_BYTES:
                    return None, "size_limit"
                with os.fdopen(file_fd, "rb", closefd=False) as stream:
                    raw = stream.read(MAX_FILE_BYTES + 1)
                after = os.fstat(file_fd)
                if len(raw) > MAX_FILE_BYTES or (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise LocalError("repository_changed_during_capture")
                return raw, "regular"
            finally:
                os.close(file_fd)
        except FileNotFoundError:
            return None, "missing"
        except OSError:
            return None, "unsupported_file_type"
        finally:
            os.close(fd)

    def capture(self, repository: str, environment: str) -> dict[str, Any]:
        head = self.run("rev-parse", "--verify", "HEAD^{commit}").decode().strip()
        paths = self._paths()
        algorithm = "sha256" if len(head) == 64 else "sha1"
        files: dict[str, Any] = {}
        total = 0
        projection_bytes = 0
        for path in paths:
            raw, kind = self._read(path)
            if raw is None:
                files[path] = {"hash": None, "coverage": kind, "symbols": []}
            else:
                total += len(raw)
                if total > MAX_TOTAL_BYTES or time.monotonic() > self.deadline:
                    raise LocalError("repository_capture_limit")
                files[path] = project_source(path, raw)
                files[path]["git_blob"] = hashlib.new(
                    algorithm, b"blob " + str(len(raw)).encode() + b"\0" + raw
                ).hexdigest()
                projection_bytes += len(str(files[path]).encode())
                if projection_bytes > 524288:
                    raise LocalError("repository_projection_limit")
                files[path]["executable"] = bool((self.root / path).lstat().st_mode & 0o111)
        # Re-read to reject observed races; no OS snapshot is claimed. Later edits
        # do not mutate this captured manifest and require a new capture.
        for path in paths:
            raw, kind = self._read(path)
            if (hashlib.sha256(raw).hexdigest() if raw is not None else None) != files[path][
                "hash"
            ] or (raw is None and kind != files[path]["coverage"]):
                raise LocalError("repository_changed_during_capture")
        if (
            head != self.run("rev-parse", "--verify", "HEAD^{commit}").decode().strip()
            or paths != self._paths()
        ):
            raise LocalError("repository_changed_during_capture")
        ancestors = (
            self.run("rev-list", f"--max-count={MAX_ANCESTORS + 1}", "HEAD").decode().splitlines()
        )
        shallow = self.run("rev-parse", "--is-shallow-repository").strip() == b"true"
        tree = self.run("ls-tree", "-r", "-z", "HEAD")
        tracked: dict[str, tuple[str, str]] = {}
        for entry in tree.split(b"\0"):
            if entry:
                meta, name = entry.split(b"\t", 1)
                mode, kind, oid = meta.decode().split()
                tracked[name.decode()] = (mode, oid if kind == "blob" else "unsupported")
        # Git blob IDs are computed locally from bytes; filters are never invoked.
        clean = set(paths) == set(tracked)
        dirty_paths = []
        for path in paths:
            expected_mode = "100755" if files[path].get("executable") else "100644"
            same = tracked.get(path) == (expected_mode, files[path].get("git_blob"))
            clean &= same
            if not same:
                dirty_paths.append(path)
        manifest = {
            "version": 1,
            "repository": repository,
            "commit": head,
            "environment": environment,
            "parser": PARSER,
            "files": files,
            "dirty": not clean,
            "dirty_paths": dirty_paths,
            "ancestors": ancestors[:MAX_ANCESTORS],
            "ancestry_complete": not shallow and len(ancestors) <= MAX_ANCESTORS,
            "ignored_untracked": "excluded",
            "atomic_filesystem_snapshot": False,
        }
        manifest["snapshot"] = "snap_" + canonical_hash(manifest)
        return manifest
