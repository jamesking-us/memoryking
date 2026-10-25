import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from memoryking.adapters.repository_git import GitSnapshot, canonical_remote
from memoryking.domain.local import LocalError
from memoryking.domain.repository import project_source


class RepositoryGitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "repo"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        (self.root / "a.py").write_text("import os\n\ndef test_ok():\n    return 1\n")
        (self.root / ".gitignore").write_text("ignored\n")
        self.commit("initial")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return (
            subprocess.check_output(["git", "-C", str(self.root), *args], stderr=subprocess.DEVNULL)
            .decode()
            .strip()
        )

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def snapshot(self):
        return GitSnapshot(self.root).capture("repo", "python-3.12-test")

    def test_identity_is_explicit_and_remote_credentials_not_retained(self):
        self.assertEqual(
            canonical_remote("git@EXAMPLE.com:Team/Repo.git"),
            canonical_remote("https://example.com/Team/Repo"),
        )
        self.assertNotEqual(canonical_remote("local:one"), canonical_remote("local:two"))
        self.assertNotEqual(
            canonical_remote("alice@example.com:repo"), canonical_remote("bob@example.com:repo")
        )
        for value in (
            "https://user:secret@example.com/r",
            "https://u@example.com/r",
            "file:///tmp/r",
            "https://example.com/a/../b",
            "https://example.com/r?secret=x",
        ):
            with self.assertRaises((LocalError, ValueError)):
                canonical_remote(value)

    def test_clean_dirty_untracked_delete_rename_and_snapshot_reproducibility(self):
        a = self.snapshot()
        self.assertFalse(a["dirty"])
        self.assertEqual(a, self.snapshot())
        (self.root / "ignored").write_text("not captured")
        self.assertEqual(a, self.snapshot())
        (self.root / "a.py").write_text("def test_ok():\n    return 2\n")
        (self.root / "new.py").write_text("value = 3\n")
        b = self.snapshot()
        self.assertTrue(b["dirty"])
        self.assertNotEqual(a["snapshot"], b["snapshot"])
        self.assertIn("new.py", b["files"])
        (self.root / "a.py").rename(self.root / "moved.py")
        c = self.snapshot()
        self.assertEqual(c["files"]["a.py"]["coverage"], "missing")
        self.assertIn("moved.py", c["files"])
        self.assertEqual(c["files"]["moved.py"]["symbols"][0]["id"], "moved.py:test_ok:1")

    def test_no_execution_hooks_filters_imports_symlinks_or_submodule_content(self):
        marker = Path(self.tmp.name) / "executed"
        (self.root / "evil.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n"
        )
        hook = self.root / ".git" / "hooks" / "fsmonitor-watchman"
        hook.write_text("#!/bin/sh\ntouch " + str(marker) + "\n")
        hook.chmod(0o700)
        self.git("config", "core.fsmonitor", str(hook))
        self.git("config", "filter.evil.clean", "touch " + str(marker))
        (self.root / ".gitattributes").write_text("*.py filter=evil\n")
        (self.root / "link.py").symlink_to(Path(self.tmp.name) / "private.py")
        (Path(self.tmp.name) / "private.py").write_text("secret = 42")
        snapshot = self.snapshot()
        self.assertFalse(marker.exists())
        self.assertEqual(snapshot["files"]["link.py"]["coverage"], "unsupported_file_type")
        self.assertIsNone(snapshot["files"]["link.py"]["hash"])
        self.assertNotIn("secret", str(snapshot["files"]["link.py"]))

    def test_branch_divergence_rebase_and_shallow_history_are_distinct(self):
        base = self.git("rev-parse", "HEAD")
        self.git("checkout", "-qb", "left")
        (self.root / "left").write_text("left")
        left = self.commit("left")
        self.git("checkout", "-qb", "right", base)
        (self.root / "right").write_text("right")
        right = self.commit("right")
        snap = self.snapshot()
        self.assertIn(base, snap["ancestors"])
        self.assertNotIn(left, snap["ancestors"])
        self.assertTrue(snap["ancestry_complete"])
        self.git("rebase", "left")
        rebased = self.snapshot()
        self.assertNotEqual(right, rebased["commit"])
        self.assertIn(left, rebased["ancestors"])
        shallow_root = Path(self.tmp.name) / "shallow"
        subprocess.check_call(
            ["git", "clone", "-q", "--depth=1", self.root.as_uri(), str(shallow_root)]
        )
        shallow = GitSnapshot(shallow_root).capture("repo", "test")
        self.assertFalse(shallow["ancestry_complete"])
        self.assertEqual(len(shallow["ancestors"]), 1)

    def test_limits_races_and_passive_parser_coverage(self):
        (self.root / "large.py").write_bytes(b"x" * (1024 * 1024 + 1))
        (self.root / "unknown.rs").write_text("fn main() {}")
        snapshot = self.snapshot()
        self.assertEqual(snapshot["files"]["large.py"]["coverage"], "size_limit")
        self.assertEqual(snapshot["files"]["unknown.rs"]["coverage"], "unsupported")
        reader = GitSnapshot(self.root)
        original = reader._read
        count = 0

        def racing(path):
            nonlocal count
            count += 1
            if count == 3:
                (self.root / "a.py").write_text("changed = True\n")
            return original(path)

        reader._read = racing
        with self.assertRaisesRegex(LocalError, "repository_changed_during_capture"):
            reader.capture("repo", "test")
        self.assertEqual(project_source("bad.py", b"if !!!")["coverage"], "parse_error")
        self.assertEqual(
            project_source("pyproject.toml", b'[project]\ndependencies=["abc>=1"]')["dependencies"][
                0
            ]["resolved"],
            False,
        )
        self.assertEqual(
            project_source("package.json", b'{"dependencies":{"a":"^2"}}')["coverage"],
            "manifest_declarations",
        )
        self.assertEqual(
            project_source("a.py", b"def test_a():\n    dynamic()")["tests"][0]["status"],
            "discovered_not_run",
        )

    def test_symlink_directory_and_fifo_are_never_followed_or_blocking(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "secret.py").write_text("secret = 42")
        (self.root / "directory.py").symlink_to(outside, target_is_directory=True)
        os.mkfifo(self.root / "pipe.py")
        snap = self.snapshot()
        # Git ls-files omits special untracked files; no data is read from them.
        self.assertNotIn("secret", str(snap))
        self.assertIsNone(snap["files"]["directory.py"]["hash"])


if __name__ == "__main__":
    unittest.main()
