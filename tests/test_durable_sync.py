"""Control-file sync selection: full flush where the platform has it, plain fsync otherwise."""

import errno
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from memoryking.adapters import local_files


class DurableSyncTests(unittest.TestCase):
    def setUp(self):
        self.stream = tempfile.TemporaryFile()
        self.fd = self.stream.fileno()
        self.addCleanup(self.stream.close)

    def test_linux_style_platform_uses_plain_fsync(self):
        with (
            mock.patch.object(local_files, "fcntl", SimpleNamespace(fcntl=mock.Mock())) as fake,
            mock.patch.object(local_files.os, "fsync") as fsync,
        ):
            local_files.durable_sync(self.fd)
        fake.fcntl.assert_not_called()
        fsync.assert_called_once_with(self.fd)

    def test_full_fsync_is_used_where_the_platform_has_it(self):
        fake = SimpleNamespace(F_FULLFSYNC=51, fcntl=mock.Mock(return_value=0))
        with (
            mock.patch.object(local_files, "fcntl", fake),
            mock.patch.object(local_files.os, "fsync") as fsync,
        ):
            local_files.durable_sync(self.fd)
        fake.fcntl.assert_called_once_with(self.fd, 51)
        fsync.assert_not_called()

    def test_unsupported_full_fsync_falls_back_and_other_errors_propagate(self):
        unsupported = SimpleNamespace(
            F_FULLFSYNC=51, fcntl=mock.Mock(side_effect=OSError(errno.ENOTSUP, "no"))
        )
        with (
            mock.patch.object(local_files, "fcntl", unsupported),
            mock.patch.object(local_files.os, "fsync") as fsync,
        ):
            local_files.durable_sync(self.fd)
        fsync.assert_called_once_with(self.fd)
        failing = SimpleNamespace(
            F_FULLFSYNC=51, fcntl=mock.Mock(side_effect=OSError(errno.EIO, "io"))
        )
        with (
            mock.patch.object(local_files, "fcntl", failing),
            mock.patch.object(local_files.os, "fsync") as fsync,
        ):
            with self.assertRaises(OSError):
                local_files.durable_sync(self.fd)
        fsync.assert_not_called()

    def test_real_descriptor_syncs(self):
        local_files.durable_sync(self.fd)
        local_files.sync_dir(Path(tempfile.gettempdir()))
        self.assertTrue(os.fstat(self.fd))
