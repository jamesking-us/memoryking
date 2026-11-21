import unittest

from alerts.model import Alert
from alerts.silence import Window, active, filter_alerts


class SilenceTests(unittest.TestCase):
    def setUp(self):
        self.windows = [Window("web-1", 100, 200)]

    def test_window_bounds(self):
        self.assertTrue(active(self.windows, "web-1", 100))
        self.assertFalse(active(self.windows, "web-1", 200))
        self.assertFalse(active(self.windows, "web-2", 150))

    def test_filter(self):
        alerts = [Alert("r", "warning", "web-1", "m"), Alert("r", "warning", "web-2", "m")]
        kept = filter_alerts(alerts, self.windows, 150)
        self.assertEqual([a.host for a in kept], ["web-2"])
        self.assertEqual(len(filter_alerts(alerts, self.windows, 250)), 2)


if __name__ == "__main__":
    unittest.main()
