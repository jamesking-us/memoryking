import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_report_document import phase_source

from memoryking.domain.local import LocalError
from memoryking.reporting.sandbox import isolated_python, macos_profile, render


@unittest.skipUnless(sys.platform == "darwin", "Native macOS isolation probe")
class ReportSandboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="memoryking-report-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.stage = self.root / "stage"
        self.stage.mkdir(mode=0o700)

    def test_real_worker_builds_complete_artifacts(self):
        (self.stage / "source.md").write_text(phase_source())
        (self.stage / "request.json").write_text(json.dumps({"title": "Synthetic phase"}))
        render(self.stage)
        result = json.loads((self.stage / "result.json").read_text())
        self.assertEqual(
            {a["name"] for a in result["artifacts"]},
            {"report.pdf", "report.epub", "reader.json", "reader-pages.json"},
        )
        self.assertGreater(result["reader_pages"], 17)
        from pypdf import PdfReader

        text = "\n".join(p.extract_text() for p in PdfReader(self.stage / "report.pdf").pages)
        for i in range(1, 18):
            self.assertIn(f"Measured result {i}.", text)

    def test_no_outside_files_network_child_process_environment_or_inherited_descriptor(self):
        secret = self.root / "synthetic-mail-secret"
        database = self.root / "synthetic-database"
        secret.write_text("synthetic secret marker")
        database.write_text("synthetic store marker")
        descriptor = os.open(secret, os.O_RDONLY)
        os.set_inheritable(descriptor, True)
        self.addCleanup(os.close, descriptor)
        code = """
import errno,json,os,socket,subprocess,sys
from pathlib import Path
result={}
for label,path in [('secret',sys.argv[1]),('database',sys.argv[2])]:
    try: Path(path).read_bytes();result[label]='leaked'
    except PermissionError: result[label]='denied'
try: socket.create_connection(('127.0.0.1',1),.1);result['network']='connected'
except OSError as e: result['network']=e.errno
try: subprocess.run(['/bin/sh','-c','true'],check=True);result['process']='executed'
except PermissionError: result['process']='denied'
try: os.read(int(sys.argv[3]),100);result['descriptor']='leaked'
except OSError as e: result['descriptor']=e.errno
result['mail_environment']=os.environ.get('GMAIL_APP_KEY')
Path('probe.json').write_text(json.dumps(result))
"""
        with patch.dict(os.environ, {"GMAIL_APP_KEY": "synthetic environment marker"}):
            isolated_python(
                self.stage,
                ["-c", code, str(secret), str(database), str(descriptor)],
                protected=[secret, database],
            )
        result = json.loads((self.stage / "probe.json").read_text())
        self.assertEqual(
            result,
            {
                "secret": "denied",
                "database": "denied",
                "network": 1,
                "process": "denied",
                "descriptor": 9,
                "mail_environment": None,
            },
        )

    def test_deadline_and_memory_monitor_stop_worker(self):
        with self.assertRaises(LocalError) as deadline:
            isolated_python(self.stage, ["-c", "import time;time.sleep(10)"], timeout=0.2)
        self.assertEqual(deadline.exception.code, "report_render_timeout")
        with self.assertRaises(LocalError) as memory:
            isolated_python(
                self.stage,
                ["-c", "import time;x=bytearray(40*1024*1024);time.sleep(10)"],
                rss_limit_kib=32 * 1024,
            )
        self.assertEqual(memory.exception.code, "report_render_memory_limit")

    def test_unsafe_layout_symlink_and_unavailable_platform_fail_closed(self):
        with self.assertRaises(LocalError):
            macos_profile(self.stage, [Path(sys.prefix) / "hypothetical-secret"])
        (self.stage / "escape.png").symlink_to(self.root / "outside")
        with self.assertRaises(LocalError):
            isolated_python(self.stage, ["-c", "pass"])
        (self.stage / "escape.png").unlink()
        for i in range(65):
            (self.stage / f"input-{i}.txt").touch()
        with self.assertRaises(LocalError) as limit:
            isolated_python(self.stage, ["-c", "pass"])
        self.assertEqual(limit.exception.code, "report_input_limit")
        with patch("memoryking.reporting.sandbox.sys.platform", "unsupported"):
            with self.assertRaises(LocalError) as unsupported:
                isolated_python(self.stage, ["-c", "pass"])
        self.assertEqual(unsupported.exception.code, "report_sandbox_unavailable")
