import unittest

from agentd import config
from agentd.runner import JobTimeout, Runner


class RunnerTests(unittest.TestCase):
    def test_retries_then_succeeds(self):
        runner = Runner(config.parse({"retries": 2}), clock=lambda: 0)
        seen = []

        def job(deadline):
            seen.append(deadline)
            if len(seen) < 3:
                raise JobTimeout()
            return "done"

        self.assertEqual(runner.run(job), "done")
        self.assertEqual(seen, [30, 30, 30])

    def test_gives_up(self):
        runner = Runner(config.parse({"retries": 1}), clock=lambda: 0)

        def job(deadline):
            raise JobTimeout()

        with self.assertRaises(JobTimeout):
            runner.run(job)

    def test_describe(self):
        text = Runner(config.parse({"timeout": 7, "retries": 1})).describe()
        self.assertEqual(text, "workers=2 timeout=7s retries=1")


if __name__ == "__main__":
    unittest.main()
