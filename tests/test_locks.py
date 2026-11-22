"""The hash locks stay installable on Windows, macOS and Linux.

pip-compile writes only the requirements whose markers are true on the machine it
runs on, so a lock compiled on macOS or Linux silently drops the Windows-only pins
that the ``.in`` files declare with a marker. These tests fail when that happens.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)\s*;\s*([^\\#\n]+?)\s*(?:\\|#|$)", re.M
)


def marked_pins(text: str) -> dict[str, tuple[str, str]]:
    return {
        name.lower(): (version, " ".join(marker.split()))
        for name, version, marker in PIN.findall(text)
    }


class LockTests(unittest.TestCase):
    def test_every_marked_pin_in_an_input_file_is_in_its_lock(self) -> None:
        inputs = sorted(ROOT.glob("requirements-*.in"))
        self.assertTrue(inputs)
        for source in inputs:
            lock = source.with_suffix(".lock")
            wanted = marked_pins(source.read_text(encoding="utf-8"))
            found = marked_pins(lock.read_text(encoding="utf-8"))
            for name, pin in wanted.items():
                with self.subTest(lock=lock.name, package=name):
                    self.assertEqual(
                        found.get(name),
                        pin,
                        f"{lock.name} lacks {name}=={pin[0]} ; {pin[1]}: compile it on Windows "
                        "(CONTRIBUTING.md, Lock files)",
                    )

    def test_every_marked_pin_in_a_lock_carries_hashes(self) -> None:
        for lock in sorted(ROOT.glob("requirements-*.lock")):
            text = lock.read_text(encoding="utf-8")
            for match in PIN.finditer(text):
                with self.subTest(lock=lock.name, package=match.group(1)):
                    following = text[match.end() : match.end() + 200]
                    self.assertIn("--hash=sha256:", following)

    def test_the_core_lock_never_needs_pywin32(self) -> None:
        core = (ROOT / "requirements-core.lock").read_text(encoding="utf-8")
        self.assertNotIn("pywin32", core)


if __name__ == "__main__":
    unittest.main()
