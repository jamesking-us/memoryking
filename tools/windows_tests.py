#!/usr/bin/env python3
"""The test suite on Windows: `unittest discover -s tests`, with the store's refusal as a skip.

Windows runs the agent-side tools but not the memory store, so every test that needs a
store here meets one refusal, `local_store_unsupported` (raised, or printed by the CLI as
JSON). This runner reports exactly that refusal as a skip, with the reason, and leaves
every other error and failure as it is. Those tests run where the store runs: on Linux,
macOS, and in WSL2 or the container.

It also writes `tmp/checks/windows-skips.json`: every skipped test and why.
"""

from __future__ import annotations

import json
import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REFUSAL = "local_store_unsupported"
REASON = "needs the memory store, which does not run on Windows (runs in WSL2 or the container)"


def refused(error: BaseException | None) -> bool:
    """The error is the store's refusal, or was caused by it."""
    seen = 0
    while error is not None and seen < 20:
        if getattr(error, "code", None) == REFUSAL:
            return True
        shown = str(error).replace('\\"', '"')
        if f'"error": "{REFUSAL}"' in shown or f"LocalError: {REFUSAL}" in shown:
            return True  # the CLI's JSON refusal, from a subprocess, possibly quoted inside
        error = error.__cause__ or error.__context__
        seen += 1
    return False


def as_skip(call):  # type: ignore[no-untyped-def]
    def wrapped(*args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            return call(*args, **kwargs)
        except unittest.SkipTest:
            raise
        except BaseException as exc:
            if refused(exc):
                raise unittest.SkipTest(REASON) from None
            raise

    return wrapped


def install() -> None:
    case = unittest.TestCase
    for name in ("_callSetUp", "_callTestMethod", "_callTearDown", "_callCleanup"):
        setattr(case, name, as_skip(getattr(case, name)))


def classes(suite: unittest.TestSuite) -> set[type]:
    found: set[type] = set()
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            found |= classes(test)
        else:
            found.add(type(test))
    return found


def main() -> int:
    if sys.platform != "win32":
        print("this runner is for Windows; elsewhere run: python -m unittest discover -s tests")
        return 2
    # As `python -m unittest` from the root has it: the root (for `tools`) and the tests.
    sys.path[:0] = [str(ROOT / "tests"), str(ROOT)]
    install()
    suite = unittest.defaultTestLoader.discover(
        str(ROOT / "tests"), top_level_dir=str(ROOT / "tests")
    )
    patched: set[type] = set()
    for cls in classes(suite):
        for owner in cls.__mro__:
            if owner in patched or owner.__module__ == "unittest.case" or owner is object:
                continue
            patched.add(owner)
            for hook in ("setUpClass", "tearDownClass"):
                if isinstance(vars(owner).get(hook), classmethod):
                    function = vars(owner)[hook].__func__
                    setattr(owner, hook, classmethod(as_skip(function)))
    verbosity = 2 if "-v" in sys.argv[1:] else 1
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    reasons = Counter(reason for _, reason in result.skipped)
    skips = {
        str(test.id() if hasattr(test, "id") else test): reason for test, reason in result.skipped
    }
    out = ROOT / "tmp" / "checks"
    out.mkdir(parents=True, exist_ok=True)
    (out / "windows-skips.json").write_text(
        json.dumps({"by_reason": dict(reasons.most_common()), "tests": skips}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"skipped {len(result.skipped)}: " + "; ".join(f"{n} {r}" for r, n in reasons.most_common())
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
