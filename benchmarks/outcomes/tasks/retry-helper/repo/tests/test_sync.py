import unittest

from tests.fakes import FakeTransport
from uplink.sync import checksum, upload
from uplink.util import retry


class SyncTests(unittest.TestCase):
    def test_upload_all_sends_files_and_manifest(self):
        transport = FakeTransport()
        sent = upload.upload_all(transport, {"b.txt": b"2", "a.txt": b"1"})
        self.assertEqual(sent, ["a.txt", "b.txt"])
        self.assertIn("MANIFEST", transport.stored)
        self.assertIn(checksum.digest(b"1"), transport.stored["MANIFEST"].decode())

    def test_manifest_is_sorted(self):
        self.assertEqual([p for p, _ in checksum.manifest({"z": b"", "a": b""})], ["a", "z"])


class RetryTests(unittest.TestCase):
    def test_retry_helper_counts(self):
        retry.reset_stats()
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 2:
                from uplink.net.errors import TransientError

                raise TransientError()
            return "ok"

        self.assertEqual(retry.call_with_retry(flaky, "demo", sleep=lambda s: None), "ok")
        self.assertEqual(retry.STATS["demo"]["retries"], 1)


if __name__ == "__main__":
    unittest.main()
