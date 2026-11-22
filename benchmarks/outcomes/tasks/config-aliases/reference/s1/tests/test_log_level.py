import unittest

from agentd import config


class LogLevelTests(unittest.TestCase):
    def test_valid_levels(self):
        for level in config.LOG_LEVELS:
            self.assertEqual(config.parse({"log_level": level}).log_level, level)

    def test_invalid_level(self):
        with self.assertRaises(config.ConfigError):
            config.parse({"log_level": "loud"})


if __name__ == "__main__":
    unittest.main()
