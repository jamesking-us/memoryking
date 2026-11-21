#!/usr/bin/env python3
"""Hidden check for staging-collector: run as `check.py WORKDIR`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import checklib  # noqa: E402

HIDDEN = '''
import unittest

from metricsd.batch import chunks
from metricsd.staging import staging_sender


def pts(n):
    return [{"name": "m", "value": i, "ts": 1000 + i} for i in range(n)]


class Hidden(unittest.TestCase):
    def test_chunks(self):
        self.assertEqual([len(c) for c in chunks(pts(120))], [50, 50, 20])
        self.assertEqual(list(chunks([])), [])
        self.assertEqual(list(chunks(pts(3), 2))[1], pts(3)[2:])
        with self.assertRaises(ValueError):
            list(chunks(pts(2), 0))

    def test_send_with_the_staging_details(self):
        calls = []
        env = {"ENABLE_COLLECTOR_V2": "1", "COLLECTOR_KEY": "s3cret"}
        send = staging_sender(env, lambda url, headers, payload: calls.append((url, headers, payload)))
        data = pts(120)
        self.assertEqual(send(data), 120)
        self.assertEqual([len(c[2]["points"]) for c in calls], [50, 50, 20])
        for url, headers, payload in calls:
            self.assertEqual(url, "http://collector.staging.internal:9411/v4/ingest")
            self.assertEqual(headers.get("X-Collector-Key"), "s3cret")
            self.assertEqual(payload["schema_version"], 4)
            self.assertEqual(set(payload), {"schema_version", "points"})
        self.assertEqual([p for c in calls for p in c[2]["points"]], data)

    def test_nothing_sent_unless_switched_on(self):
        calls = []
        post = lambda *a: calls.append(a)
        for env in ({}, {"COLLECTOR_KEY": "k"}, {"ENABLE_COLLECTOR_V2": "0", "COLLECTOR_KEY": "k"}):
            self.assertEqual(staging_sender(env, post)(pts(5)), 0)
        self.assertEqual(calls, [])

unittest.main()
'''


def main() -> int:
    workdir = checklib.workdir_arg()
    report = checklib.Report()
    ok, detail = checklib.hidden_tests(workdir, HIDDEN)
    report.add("hidden tests", ok, detail)
    return report.finish()


if __name__ == "__main__":
    raise SystemExit(main())
