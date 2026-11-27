"""The local daemon's accept thread never waits on busy workers; waiting is bounded."""

import socket
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore

BUSY = b'{"error":"service_busy"}\n'


class SmallServer(Server):
    max_concurrent = 2
    max_waiting = 3
    wait_seconds = 1


class Blocking:
    def __init__(self):
        self.release = threading.Event()

    def execute(self, value):
        self.release.wait(10)
        return {"ok": True}


class BusyDaemonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mk-ipc-", dir="/tmp")
        base = Path(self.temp.name)
        LocalStore.initialize(base / "store", base / "control")
        self.store = LocalStore(base / "store", base / "control")
        self.server = SmallServer(self.store)
        self.blocking = Blocking()
        self.server.operations = self.blocking
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.blocking.release.set()
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.store.close()
        self.temp.cleanup()

    def connect(self):
        stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stream.settimeout(10)
        self.addCleanup(stream.close)
        stream.connect(str(self.server.path))
        stream.sendall(b"{}\n")
        return stream

    def reply(self, stream):
        started = time.monotonic()
        data = stream.makefile("rb").readline()
        return data, time.monotonic() - started

    def test_waiting_connections_wait_together_and_the_wait_is_bounded(self):
        holders = [self.connect() for _ in range(2)]
        time.sleep(0.3)  # Both workers are now inside the blocked operation.
        started = time.monotonic()
        waiters = [self.connect() for _ in range(3)]
        beyond = self.connect()
        # Past the waiting bound: refused at once, so the accept thread did not stall.
        data, elapsed = self.reply(beyond)
        self.assertEqual(BUSY, data)
        self.assertLess(elapsed, 0.5)
        # The three waiters time out together after about one wait, not one after another.
        for stream in waiters:
            self.assertEqual(BUSY, self.reply(stream)[0])
        self.assertGreaterEqual(time.monotonic() - started, 0.9)
        self.assertLess(time.monotonic() - started, 2.0)
        # The daemon still accepts and serves once a worker frees up.
        self.blocking.release.set()
        for stream in holders:
            self.assertEqual(b'{"ok":true}\n', self.reply(stream)[0])
        self.assertEqual(b'{"ok":true}\n', self.reply(self.connect())[0])

    def test_a_stop_signal_that_lands_while_a_worker_starts_still_stops_the_daemon(self):
        # SIGTERM is delivered as KeyboardInterrupt on the accept thread. When it lands just after
        # the worker thread started, that thread owns the slot: the accept thread's cleanup must
        # not release it too. A second release raised ValueError, which replaced the interrupt, so
        # the accept loop carried on and the daemon never stopped (`daemon_stop_failed`).
        import socketserver

        real = socketserver.ThreadingMixIn.process_request

        def interrupted(server, request, client_address):
            real(server, request, client_address)  # the worker thread is started
            raise KeyboardInterrupt

        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        self.blocking.release.set()
        with unittest.mock.patch.object(
            socketserver.ThreadingMixIn, "process_request", interrupted
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.server.process_request(left, "")
        deadline = time.monotonic() + 5
        while self.server.saturation()["local_workers"][1] and time.monotonic() < deadline:
            time.sleep(0.01)
        # Every slot is back, once: the limit is neither exceeded nor leaked.
        self.assertEqual(0, self.server.saturation()["local_workers"][1])
        self.assertEqual(self.server.max_concurrent, self.server.slots._value)


if __name__ == "__main__":
    unittest.main()
