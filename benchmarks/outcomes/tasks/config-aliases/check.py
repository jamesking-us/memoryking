#!/usr/bin/env python3
"""Hidden check for config-aliases: run as `check.py WORKDIR`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import checklib  # noqa: E402

HIDDEN = '''
import json, os, subprocess, sys, tempfile, unittest, warnings

from agentd import config
from agentd.runner import JobTimeout, Runner

warnings.simplefilter("ignore")


class Hidden(unittest.TestCase):
    def test_new_names(self):
        s = config.parse({"timeout_seconds": 12, "max_retries": 5})
        self.assertEqual((s.timeout_seconds, s.max_retries), (12, 5))
        d = config.parse({})
        self.assertEqual((d.timeout_seconds, d.max_retries, d.workers), (30, 3, 2))

    def test_old_names_still_load(self):
        s = config.parse({"timeout": 9, "retries": 1, "workers": 3})
        self.assertEqual((s.timeout_seconds, s.max_retries, s.workers), (9, 1, 3))

    def test_old_file_on_disk_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "prod.json")
            with open(path, "w") as handle:
                json.dump({"timeout": 45, "retries": 0, "log_level": "warning"}, handle)
            s = config.load(path)
            self.assertEqual((s.timeout_seconds, s.max_retries, s.log_level), (45, 0, "warning"))
            proc = subprocess.run(
                [sys.executable, "-W", "ignore", "-m", "agentd.cli", "--config", path],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("timeout=45s retries=0", proc.stdout)

    def test_new_spelling_wins(self):
        s = config.parse({"timeout": 9, "timeout_seconds": 20, "retries": 1, "max_retries": 4})
        self.assertEqual((s.timeout_seconds, s.max_retries), (20, 4))

    def test_still_strict(self):
        with self.assertRaises(config.ConfigError):
            config.parse({"bogus": 1})
        with self.assertRaises(config.ConfigError):
            config.parse({"timeout": 0})
        with self.assertRaises(config.ConfigError):
            config.parse({"log_level": "loud"})
        self.assertEqual(config.parse({"log_level": "error"}).log_level, "error")

    def test_runner_uses_new_values(self):
        runner = Runner(config.parse({"retries": 2, "timeout": 3}), clock=lambda: 100)
        seen = []

        def job(deadline):
            seen.append(deadline)
            if len(seen) < 3:
                raise JobTimeout()
            return "ok"

        self.assertEqual(runner.run(job), "ok")
        self.assertEqual(seen, [103, 103, 103])

unittest.main()
'''


def main() -> int:
    workdir = checklib.workdir_arg()
    report = checklib.Report()
    ok, detail = checklib.hidden_tests(workdir, HIDDEN)
    report.add("hidden tests", ok, detail)
    example = workdir / "examples" / "agentd.json"
    text = example.read_text() if example.exists() else ""
    report.add(
        "example file uses the new names",
        '"timeout_seconds"' in text and '"max_retries"' in text,
        text,
    )
    return report.finish()


if __name__ == "__main__":
    raise SystemExit(main())
