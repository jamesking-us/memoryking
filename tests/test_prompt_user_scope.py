"""The prompt-time hook and the user's own memory, end to end on a real local service.

A standing preference saved in the user's own scope reaches a prompt in any project the agent
works in, under the authorization the briefing uses; a project's own facts stay in the project."""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from memoryking import hooks, host_hooks
from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore

PREFERENCE = "Whatever the project, I like dotted day.month.year as the date format everywhere."
FACT_A = "In project alpha the report page date column uses a weekly grid."
PROMPT = "Implement the date column for the report page and follow what we settled for it."


class UserScopePromptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-us-")
        self.root = Path(self.temp.name)
        self.owner_file = LocalStore.initialize(self.root / "db", self.root / "ctl")
        self.owner = self.owner_file.read_text().strip()
        self.store = LocalStore(self.root / "db", self.root / "ctl")
        self.server = Server(self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        caps = ["capture", "read"]
        self.agent = self.write(
            "agent", self.store.grant(self.owner, "agent", "alpha", caps, for_user="@self")
        )
        self.store.grant(self.owner, "agent", "beta", caps)
        # an agent that acts for no user at all
        self.loner = self.write("loner", self.store.grant(self.owner, "loner", "alpha", caps))
        self.store.grant(self.owner, "loner", "beta", caps)
        hooks.capture(
            PREFERENCE, source="remember", scope="user", **self.where(self.agent, "alpha")
        )
        hooks.capture(FACT_A, source="decision", **self.where(self.agent, "alpha"))

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.store.close()
        self.temp.cleanup()

    def write(self, name, token):
        path = self.root / f"{name}.credential"
        path.write_text(token)
        path.chmod(0o600)
        return path

    def where(self, credential, project):
        return {"socket": self.server.path, "credential": credential, "project": project}

    def submit(self, credential, project, prompt=PROMPT, **options):
        event = host_hooks.read_event(
            json.dumps(
                {"session_id": "s", "hook_event_name": "UserPromptSubmit", "prompt": prompt}
            ),
            {},
        )
        return host_hooks.prompt(event, **self.where(credential, project), **options)

    def test_a_preference_saved_in_one_project_reaches_a_prompt_in_another(self):
        shown = self.submit(self.agent, "beta")
        self.assertIn("dotted day.month.year", shown)
        self.assertIn("from your own user memory", shown)
        self.assertIn("evidence, not instructions", shown)

    def test_a_projects_own_fact_never_reaches_another_project(self):
        in_alpha = self.submit(self.agent, "alpha")
        self.assertIn("weekly grid", in_alpha)
        self.assertIn("dotted day.month.year", in_alpha)
        in_beta = self.submit(self.agent, "beta")
        self.assertNotIn("weekly grid", in_beta)
        self.assertNotIn("alpha", in_beta)

    def test_a_credential_that_acts_for_no_user_is_shown_none_of_it(self):
        for project in ("alpha", "beta"):
            self.assertNotIn("dotted day.month.year", self.submit(self.loner, project))
        self.assertIn("weekly grid", self.submit(self.loner, "alpha"))

    def test_the_hook_stays_inside_its_budget_and_its_deadline_with_both_lookups(self):
        import time

        started = time.monotonic()
        shown = self.submit(self.agent, "alpha")
        self.assertLess(time.monotonic() - started, host_hooks.PROMPT_DEADLINE)
        self.assertLessEqual(
            len(shown), host_hooks.PROMPT_BUDGET * host_hooks.PROMPT_CHARS_PER_TOKEN
        )

    def test_a_purge_of_the_users_memory_takes_the_preference_out_of_the_hook(self):
        self.assertIn("dotted day.month.year", self.submit(self.agent, "beta"))
        from memoryking.adapters.local_ipc import request

        request(self.server.path, self.owner, "user", {"action": "purge"})
        self.assertNotIn("dotted day.month.year", self.submit(self.agent, "beta"))


if __name__ == "__main__":
    unittest.main()
