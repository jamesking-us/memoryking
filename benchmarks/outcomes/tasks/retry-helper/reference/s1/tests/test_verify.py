import unittest

from uplink.sync import checksum


class VerifyTests(unittest.TestCase):
    def test_verify_manifest(self):
        files = {"a": b"1", "b": b"2"}
        text = "\n".join(f"{p} {d}" for p, d in checksum.manifest(files))
        self.assertEqual(checksum.verify_manifest(files, text), [])
        self.assertEqual(checksum.verify_manifest({**files, "b": b"x"}, text), ["b"])
        self.assertEqual(checksum.verify_manifest({"a": b"1"}, text), ["b"])


if __name__ == "__main__":
    unittest.main()
