import unittest

from metricsd.staging import staging_sender

ENV = {"ENABLE_COLLECTOR_V2": "1", "COLLECTOR_KEY": "k"}


class StagingTests(unittest.TestCase):
    def test_sends_batches(self):
        calls = []
        send = staging_sender(ENV, lambda url, headers, payload: calls.append((url, headers, payload)))
        points = [{"name": "n", "value": i, "ts": i} for i in range(60)]
        self.assertEqual(send(points), 60)
        self.assertEqual([len(c[2]["points"]) for c in calls], [50, 10])
        self.assertEqual(calls[0][0], "http://collector.staging.internal:9411/v4/ingest")
        self.assertEqual(calls[0][2]["schema_version"], 4)

    def test_off(self):
        calls = []
        send = staging_sender({}, lambda *a: calls.append(a))
        self.assertEqual(send([{"name": "n", "value": 1, "ts": 1}]), 0)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
