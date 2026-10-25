"""Real current-authorized SQLite task projection, including evidence regressions."""

import json
import unittest

import test_knowledge as knowledge_helpers
import test_working as working_helpers
from test_working import checkpoint

from memoryking.adapters.dashboard import Dashboard
from memoryking.adapters.execution import Execution
from memoryking.contracts.canonical import canonical_bytes


class ExecutionTests(unittest.TestCase):
    setUp = knowledge_helpers.KnowledgeTests.setUp
    tearDown = knowledge_helpers.KnowledgeTests.tearDown
    scope = working_helpers.WorkingTests.scope
    command = working_helpers.WorkingTests.command
    execute = working_helpers.WorkingTests.execute
    save = working_helpers.WorkingTests.save

    def view(self, credential=None, offset=0):
        with self.store.transaction() as decision:
            at = decision.evaluated_ms + offset
            who = self.store._auth(credential or self.owner, "read", "p", at)
            return Execution(self.store).snapshot(who, "p", at)

    def passing(self):
        body = checkpoint()
        body["acceptance"][0].update(
            state="passed", evidence=[{"kind": "observation", "id": self.obs["observation"]}]
        )
        return body

    def event(self, source, event_id, saved, **kw):
        return self.store.capture(
            self.owner,
            self.ns,
            event_id,
            {
                "version": 1,
                "project": "p",
                "source": source,
                "source_event": event_id,
                "text": canonical_bytes(
                    {
                        "version": 1,
                        "id": event_id,
                        "project": "p",
                        "task": "task",
                        "run": None,
                        "checkpoint": saved["checkpoint"],
                        "revision": saved["revision"],
                        "kind": "checkpoint",
                        "activity": "idle",
                        **kw,
                    }
                ).decode(),
                "classification": "internal",
                "readers": None,
                "observed_at_ms": 1,
            },
        )

    def test_progress_denominator_and_reopening_are_snapshot_counts(self):
        body = self.passing()
        saved = self.save(body)
        self.assertEqual(self.view()["tasks"][0]["progress"]["passed"], 1)
        body["acceptance"][0]["state"] = "pending"
        body["acceptance"].append(
            {"id": "new", "text": "New scope", "state": "pending", "evidence": [], "decision": None}
        )
        self.save(body, revision=saved["revision"])
        progress = self.view()["tasks"][0]["progress"]
        self.assertEqual(
            (
                progress["passed"],
                progress["pending"],
                progress["total"],
                progress["previous_total"],
                progress["reopened"],
            ),
            (0, 2, 2, 1, 1),
        )
        self.assertTrue(progress["scope_changed"])

    def test_changed_definition_keeps_old_proof_unknown_after_later_checkpoints(self):
        body = self.passing()
        self.save(body)
        body["acceptance"][0]["text"] = "Different requirement"
        self.save(body, revision=1)
        self.save(body, revision=2)
        task = self.view()["tasks"][0]
        self.assertEqual(task["progress"]["unknown"], 1)
        self.assertTrue(task["criteria"][0]["reverification_required"])
        fresh = self.event("test", "new-proof", {"checkpoint": "none", "revision": 0})
        body["acceptance"][0]["evidence"] = [{"kind": "observation", "id": fresh["observation"]}]
        self.save(body, revision=3)
        self.assertEqual(self.view()["tasks"][0]["progress"]["passed"], 1)

    def test_missing_physical_evidence_demotes_pass(self):
        self.save(self.passing())
        row = self.store.db.execute(
            "SELECT * FROM observations WHERE id=?", (self.obs["observation"],)
        ).fetchone()
        (self.store.root / "blobs" / row["tenant"] / row["blob"]).unlink()
        task = self.view()["tasks"][0]
        self.assertEqual(task["progress"]["unknown"], 1)
        self.assertFalse(task["criteria"][0]["evidence_available"])

    def test_source_restriction_removes_copied_titles_and_purge_removes_task(self):
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        self.save(self.passing(), readers=["owner", "reader"])
        self.assertEqual(len(self.view(reader)["tasks"]), 1)
        self.store.db.execute(
            "UPDATE observations SET readers=? WHERE id=?",
            (json.dumps(["owner"]), self.obs["observation"]),
        )
        hidden = self.view(reader)
        self.assertEqual(hidden["status"], "unavailable")
        self.assertEqual(hidden["tasks"], [])
        self.assertNotIn("timeout", json.dumps(hidden))
        self.store.purge(self.owner, self.obs["observation"])
        self.assertEqual(self.view()["tasks"], [])

    def test_heartbeat_uses_recorded_time_and_current_checkpoint_without_progress(self):
        saved = self.save()
        self.event("reference-heartbeat-v1", "beat", saved, observed_ms=9999999999999)
        task = self.view()["tasks"][0]
        self.assertEqual(task["heartbeat"]["status"], "recent")
        self.assertEqual(task["revision"], 1)
        self.assertEqual(task["progress"]["pending"], 1)
        self.assertEqual(self.view(offset=120001)["tasks"][0]["heartbeat"]["status"], "stale")
        self.save(revision=1)
        self.assertEqual(self.view()["tasks"][0]["heartbeat"]["status"], "unknown")

    def test_events_require_checkpoint_lineage_and_duplicate_retries_do_not_inflate(self):
        saved = self.save()
        observation = self.event("reference-harness-v1", "event1", saved)
        self.assertEqual(
            self.event("reference-harness-v1", "event1", saved)["observation"],
            observation["observation"],
        )
        self.assertEqual(self.view()["events"], [])
        body = checkpoint()
        body["references"] = [{"kind": "observation", "id": observation["observation"]}]
        self.save(body, revision=1)
        view = self.view()
        self.assertEqual(len(view["events"]), 1)
        self.assertEqual(view["events"][0]["actor"], "owner")
        self.assertEqual(view["tasks"][0]["progress"]["total"], 1)
        self.assertNotIn("details", view["events"][0])

    def test_malformed_telemetry_does_not_break_snapshot(self):
        saved = self.save()
        observation = self.event("reference-harness-v1", "bad-kind", saved, kind=[])
        self.event("reference-heartbeat-v1", "bad-activity", saved, activity=[])
        body = checkpoint()
        body["references"] = [{"kind": "observation", "id": observation["observation"]}]
        self.save(body, revision=1)
        self.assertEqual(self.view()["events"], [])

    def test_hidden_checkpoint_does_not_reveal_task_counts(self):
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        self.save()
        self.assertEqual(self.view(reader)["tasks"], [])

    def test_bounded_history_never_invents_verified_credit(self):
        saved = self.save(self.passing())
        # Populate a long history fixture using valid identical persisted bodies;
        # this verifies read-model bounds, not checkpoint mutation conformance.
        row = dict(
            self.store.db.execute(
                "SELECT * FROM checkpoints WHERE id=?", (saved["checkpoint"],)
            ).fetchone()
        )
        self.store.db.execute(
            "UPDATE checkpoints SET revision=130 WHERE id=?", (saved["checkpoint"],)
        )
        self.store.db.execute("UPDATE checkpoint_streams SET revision=130")
        columns = ",".join(row)
        placeholders = ",".join("?" for _ in row)
        for revision in range(1, 130):
            prior = {**row, "id": f"history_{revision}", "revision": revision}
            self.store.db.execute(
                f"INSERT INTO checkpoints ({columns}) VALUES ({placeholders})",
                tuple(prior.values()),
            )
        task = self.view()["tasks"][0]
        self.assertFalse(task["progress"]["history_complete"])
        self.assertEqual(task["progress"]["unknown"], 1)

    def test_http_execution_is_session_authenticated_and_readonly(self):
        import http.client
        from pathlib import Path

        self.save()
        web = Dashboard(self.store, Path(__file__).resolve().parents[1])
        web.start()
        try:

            def call(method, path, body=None, headers=None):
                conn = http.client.HTTPConnection("127.0.0.1", web.server_port)
                conn.request(method, path, body, headers or {})
                response = conn.getresponse()
                result = response.status, dict(response.headers), response.read()
                conn.close()
                return result

            self.assertEqual(call("GET", "/api/execution/p")[0], 403)
            code = web.issue(self.owner)["code"]
            login = call(
                "POST",
                "/api/bootstrap",
                json.dumps({"code": code}),
                {"Origin": web.origin, "Content-Type": "application/json"},
            )
            headers = {"Cookie": login[1]["Set-Cookie"].split(";")[0]}
            response = call("GET", "/api/execution/p", headers=headers)
            self.assertEqual(response[0], 200, response[2])
            self.assertEqual(json.loads(response[2])["tasks"][0]["revision"], 1)
            self.assertEqual(call("HEAD", "/api/execution/p", headers=headers)[2], b"")
            self.assertEqual(call("GET", "/api/execution/p?private=yes", headers=headers)[0], 403)
            self.assertEqual(
                call(
                    "GET", "/api/execution/p", headers={**headers, "Origin": "https://hostile.test"}
                )[0],
                403,
            )
            self.assertEqual(call("POST", "/api/execution/p", "{}", headers)[0], 403)
        finally:
            web.stop()
