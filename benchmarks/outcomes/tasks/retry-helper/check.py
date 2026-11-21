#!/usr/bin/env python3
"""Hidden check for retry-helper: run as `check.py WORKDIR`."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import checklib  # noqa: E402

HIDDEN = '''
import unittest
from unittest import mock

from uplink.net.errors import PermanentError, TransientError
from uplink.net.transport import Transport
from uplink.sync import checksum, upload
from uplink.util import retry


class Flaky(Transport):
    def __init__(self, failures):
        self.failures = {k: list(v) for k, v in failures.items()}
        self.calls = []
        self.stored = {}

    def put(self, path, data):
        self.calls.append(path)
        queue = self.failures.get(path)
        if queue:
            raise queue.pop(0)
        self.stored[path] = data


class Hidden(unittest.TestCase):
    def setUp(self):
        retry.reset_stats()
        patcher = mock.patch("time.sleep", lambda s: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_verify_manifest(self):
        files = {"a": b"1", "b": b"2", "c": b"3"}
        text = "\\n".join(f"{p} {d}" for p, d in checksum.manifest(files))
        self.assertEqual(checksum.verify_manifest(files, text), [])
        self.assertEqual(checksum.verify_manifest({**files, "b": b"x"}, text), ["b"])
        self.assertEqual(checksum.verify_manifest({"a": b"1", "c": b"3"}, text), ["b"])
        self.assertEqual(checksum.verify_manifest({**files, "d": b"4"}, text), ["d"])

    def test_survives_three_transient_failures_per_put(self):
        t = Flaky({"a": [TransientError()] * 3, "MANIFEST": [TransientError()] * 3})
        sent = upload.upload_all(t, {"a": b"1", "b": b"2"})
        self.assertEqual(sent, ["a", "b"])
        self.assertEqual(t.calls.count("a"), 4)
        self.assertEqual(t.calls.count("MANIFEST"), 4)
        self.assertIn("MANIFEST", t.stored)

    def test_gives_up_after_four_attempts(self):
        t = Flaky({"a": [TransientError()] * 9})
        with self.assertRaises(TransientError):
            upload.upload_all(t, {"a": b"1"})
        self.assertEqual(t.calls.count("a"), 4)

    def test_permanent_error_is_immediate(self):
        t = Flaky({"a": [PermanentError()] * 9})
        with self.assertRaises(PermanentError):
            upload.upload_all(t, {"a": b"1"})
        self.assertEqual(t.calls.count("a"), 1)

    def test_goes_through_the_counted_helper(self):
        t = Flaky({"a": [TransientError()] * 2})
        upload.upload_all(t, {"a": b"1"})
        self.assertGreaterEqual(sum(e["calls"] for e in retry.STATS.values()), 2)
        self.assertEqual(sum(e["retries"] for e in retry.STATS.values()), 2)

unittest.main()
'''


def main() -> int:
    workdir = checklib.workdir_arg()
    report = checklib.Report()
    ok, detail = checklib.hidden_tests(workdir, HIDDEN)
    report.add("hidden tests", ok, detail)
    offenders = []
    for path in (workdir / "uplink").rglob("*.py"):
        rel = path.relative_to(workdir).as_posix()
        if rel == "uplink/net/backoff.py":
            continue
        text = path.read_text()
        if re.search(r"\bbackoff\b", text) and rel != "uplink/util/retry.py":
            offenders.append(rel)
    report.add("net/backoff.py not used", not offenders, ", ".join(offenders))
    loops = [
        p.relative_to(workdir).as_posix()
        for p in (workdir / "uplink" / "sync").glob("*.py")
        if re.search(r"\bsleep\(", p.read_text())
    ]
    report.add("no hand-written retry loop in sync/", not loops, ", ".join(loops))
    return report.finish()


if __name__ == "__main__":
    raise SystemExit(main())
