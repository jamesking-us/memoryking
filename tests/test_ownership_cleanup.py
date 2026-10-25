import os
import tempfile
import unittest
from pathlib import Path

from memoryking.adapters.local_files import Ownership
from memoryking.domain.local import LocalError


class OwnershipCleanupTests(unittest.TestCase):
    def test_repeated_cleanup_does_not_close_reused_descriptor(self):
        with tempfile.TemporaryDirectory() as temp:
            owner = Ownership(Path(temp))
            original = owner.fd
            owner.close()
            held = os.open(Path(temp) / "unrelated", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                self.assertEqual(held, original)
                owner.close()
                os.write(held, b"still open")
                with self.assertRaisesRegex(LocalError, "owner_closed"):
                    owner.check()
            finally:
                os.close(held)
