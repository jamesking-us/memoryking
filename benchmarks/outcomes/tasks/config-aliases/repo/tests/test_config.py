import json
import os
import tempfile
import unittest

from agentd import config


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        s = config.parse({})
        self.assertEqual((s.timeout, s.retries, s.workers, s.log_level), (30, 3, 2, "info"))

    def test_override(self):
        self.assertEqual(config.parse({"timeout": 5}).timeout, 5)

    def test_unknown_key_rejected(self):
        with self.assertRaises(config.ConfigError):
            config.parse({"bogus": 1})

    def test_validation(self):
        with self.assertRaises(config.ConfigError):
            config.parse({"timeout": 0})
        with self.assertRaises(config.ConfigError):
            config.parse({"retries": -1})

    def test_load_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.json")
            with open(path, "w") as handle:
                json.dump({"workers": 8}, handle)
            self.assertEqual(config.load(path).workers, 8)
            with self.assertRaises(config.ConfigError):
                config.load(os.path.join(tmp, "missing.json"))


if __name__ == "__main__":
    unittest.main()
