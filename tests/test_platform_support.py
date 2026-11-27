"""The platform module: unchanged POSIX primitives, and explicit refusals on Windows."""

from __future__ import annotations

import importlib
import json
import os
import pkgutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import memoryking
from memoryking.adapters import platform_support
from memoryking.adapters.local_files import atomic, private_dir, private_file, sync_dir
from memoryking.domain.hints import hint
from memoryking.domain.local import LocalError

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = sys.platform == "win32"


class EveryPlatformTests(unittest.TestCase):
    def test_every_module_imports(self) -> None:
        for module in pkgutil.walk_packages(memoryking.__path__, "memoryking."):
            with self.subTest(module=module.name):
                importlib.import_module(module.name)

    def test_the_refusal_has_a_short_hint_that_names_both_back_ends(self) -> None:
        text = hint(platform_support.STORE_UNSUPPORTED)
        self.assertLessEqual(len(text), 200)
        self.assertIn("WSL2", text)
        self.assertIn("Docker Desktop", text)

    def test_a_liveness_check_sees_a_process_and_its_end_and_interrupts_nobody(self) -> None:
        self.assertTrue(platform_support.process_alive(os.getpid()))
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.assertTrue(platform_support.process_alive(child.pid))
        finally:
            child.kill()
            child.wait()
        self.assertFalse(platform_support.process_alive(child.pid))
        # Reaching here at all means no Ctrl-C reached this process (Windows signal 0).

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "needs /proc to read a process state")
    def test_a_process_that_has_exited_but_is_not_yet_collected_is_not_alive(self) -> None:
        # An orphaned daemon is a zombie until init collects it, and `kill(pid, 0)` still
        # succeeds for it: a stop that waited for that would fail on a machine or container
        # whose process 1 is slow to reap or never reaps.
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            stat = Path(f"/proc/{child.pid}/stat")
            for _ in range(500):
                if stat.read_text().rpartition(")")[2].strip().startswith("Z"):
                    break
                time.sleep(0.02)
            else:
                self.fail("the child never became a zombie")
            os.kill(child.pid, 0)  # the old check: still "alive"
            self.assertFalse(platform_support.process_alive(child.pid))
        finally:
            child.wait()


@unittest.skipIf(WINDOWS, "POSIX primitives; Windows refuses them (WindowsTests)")
class PosixTests(unittest.TestCase):
    def test_a_store_is_supported_and_nothing_refuses(self) -> None:
        self.assertTrue(platform_support.LOCAL_STORE_SUPPORTED)
        platform_support.require_local_store()

    def test_owner_is_the_process_uid(self) -> None:
        self.assertTrue(platform_support.owned_by_current_user(os.getuid()))
        self.assertFalse(platform_support.owned_by_current_user(os.getuid() + 1))

    def test_the_lock_is_exclusive_across_descriptors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.lock"
            first = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            second = os.open(path, os.O_RDWR)
            try:
                platform_support.lock_nowait(first)
                with self.assertRaises(BlockingIOError):
                    platform_support.lock_nowait(second)
            finally:
                os.close(first)
                os.close(second)

    def test_opens_refuse_a_symbolic_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real, link = Path(tmp) / "real", Path(tmp) / "link"
            real.mkdir()
            (real / "f").write_bytes(b"x")
            link.symlink_to(real)
            for opener in (
                lambda: platform_support.open_nofollow(link, os.O_RDONLY),
                lambda: platform_support.open_directory_nofollow(link),
                lambda: platform_support.open_regular_nofollow(link),
            ):
                with self.assertRaises(OSError):
                    os.close(opener())
            os.close(platform_support.open_directory_nofollow(real))
            os.close(platform_support.open_regular_nofollow(real / "f"))


@unittest.skipUnless(WINDOWS, "Windows refusals")
class WindowsTests(unittest.TestCase):
    def assert_refused(self, call: object) -> None:
        with self.assertRaises(LocalError) as caught:
            call()  # type: ignore[operator]
        self.assertEqual(caught.exception.code, platform_support.STORE_UNSUPPORTED)

    def test_store_primitives_refuse_before_touching_the_disk(self) -> None:
        self.assertFalse(platform_support.LOCAL_STORE_SUPPORTED)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assert_refused(lambda: private_dir(root / "new", create=True))
            self.assert_refused(lambda: private_file(root / "missing"))
            self.assert_refused(lambda: atomic(root / "file", b"x"))
            self.assert_refused(lambda: sync_dir(root))
            self.assert_refused(lambda: platform_support.owned_by_current_user(0))
            self.assert_refused(lambda: platform_support.lock_nowait(0))
            self.assert_refused(lambda: platform_support.open_nofollow(root, os.O_RDONLY))
            self.assert_refused(platform_support.unix_stream_socket)
            self.assertEqual(sorted(p.name for p in root.iterdir()), [])

    def test_commands_that_open_a_store_refuse_with_the_hint(self) -> None:
        for command in (
            ["init", "--store", "s", "--control", "c"],
            ["serve", "--store", "s", "--control", "c"],
            ["gc", "--store", "s", "--control", "c"],
            ["journal", "pending", "--store", "s", "--control", "c"],
        ):
            with self.subTest(command=command[0]), tempfile.TemporaryDirectory() as tmp:
                result = subprocess.run(
                    [sys.executable, "-m", "memoryking", *command],
                    cwd=tmp,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=60,
                )
                self.assertEqual(result.returncode, 2)
                reply = json.loads(result.stderr)
                self.assertEqual(reply["error"], platform_support.STORE_UNSUPPORTED)
                self.assertEqual(reply["hint"], hint(platform_support.STORE_UNSUPPORTED))
                self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
