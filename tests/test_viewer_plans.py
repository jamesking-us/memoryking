"""Plan views carry the same project/session/temporal disclosure boundary as memory."""

import copy
import json
import unittest
from dataclasses import replace

import test_knowledge as knowledge
from test_plans import plan
from test_postgres_parity import NativeFixture
from test_viewer_reads import ReadCases

from memoryking.adapters.working import Working
from memoryking.domain.local import LocalError


class ViewerPlanCases:
    command = ReadCases.command
    policy = ReadCases.policy
    bind = ReadCases.bind
    grant_viewer = ReadCases.grant_viewer
    subject = ReadCases.subject
    actor_store = ReadCases.actor_store
    setup_viewer = ReadCases.setup_viewer
    read = ReadCases.read

    def save(self, body=None, revision=0, task="checkout", readers=None):
        self.n += 1
        command = {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": "plan_save",
            "key": f"viewer-plan-{self.n}",
            "args": {
                "project": "p",
                "task": task,
                "expected_revision": revision,
                "readers": readers or ["owner", "phone"],
                "plan": body or plan(),
            },
        }
        Working(self.store).execute(self.owner, command).emit(lambda _: None)

    def test_plan_list_phase_steps_progress_and_evidence_current_read(self):
        self.setup_viewer()
        value = plan()
        value["steps"][0].update(
            status="done", evidence=[{"kind": "observation", "id": self.obs["observation"]}]
        )
        self.save(value)
        result = self.read("plans")
        self.assertEqual(result["data"]["plans"][0]["progress"]["completed"], 1)
        detail = self.read("plan", params={"plan": "checkout", "phase": "design"})
        body = detail["data"]["plan"]
        self.assertEqual(body["progress"]["total"], 2)
        self.assertEqual(len(body["steps"]), 1)
        proof = body["steps"][0]["evidence"][0]
        self.assertIn(
            self.raw,
            self.read("memory", params={"handle": proof["handle"]})["data"]["memories"][0]["text"],
        )
        self.assertNotIn('"writer"', json.dumps(result))
        self.assertLessEqual(
            detail["binding"]["fresh_until_ms"] - detail["binding"]["evaluated_ms"], 15000
        )

    def test_plan_cursor_binds_phase_session_and_mutations(self):
        self.setup_viewer()
        self.save()
        first = self.read("plan", params={"plan": "checkout", "limit": 1})
        cursor = first["next_cursor"]
        self.assertIsNotNone(cursor)
        next_page = self.read("plan", params={"plan": "checkout", "limit": 1, "cursor": cursor})
        self.assertEqual(next_page["data"]["plan"]["steps"][0]["id"], "test")
        for params, session in [
            ({"plan": "checkout", "limit": 1, "phase": "design", "cursor": cursor}, self.session),
            (
                {"plan": "checkout", "limit": 1, "cursor": cursor},
                replace(self.session, identity="other"),
            ),
        ]:
            with self.assertRaises(LocalError):
                self.read("plan", params=params, session=session)
        self.save(revision=1)
        with self.assertRaises(LocalError):
            self.read("plan", params={"plan": "checkout", "limit": 1, "cursor": cursor})

    def test_reader_reduction_revocation_and_hidden_plan_do_not_leak(self):
        self.setup_viewer()
        self.save()
        self.save(task="hidden", readers=["owner"])
        self.assertEqual(len(self.read("plans")["data"]["plans"]), 1)
        with self.assertRaises(LocalError):
            self.read("plan", params={"plan": "hidden"})
        self.save(revision=1, readers=["owner"])
        self.assertEqual(self.read("plans")["data"]["plans"], [])
        with self.assertRaises(LocalError):
            self.read("plan", params={"plan": "checkout"})
        self.policy("revoke", {"principal": "phone", "project": "p", "expected_version": 2})
        with self.assertRaises(LocalError):
            self.read("plans")

    def test_many_steps_paginate_without_losing_denominator(self):
        self.setup_viewer()
        value = plan()
        base = value["steps"][0]
        value["steps"] = [{**copy.deepcopy(base), "id": f"step{i}"} for i in range(70)]
        self.save(value)
        cursor = None
        ids = []
        for _ in range(4):
            result = self.read(
                "plan",
                params={"plan": "checkout", "limit": 50, **({"cursor": cursor} if cursor else {})},
            )
            self.assertEqual(result["data"]["plan"]["progress"]["total"], 70)
            ids.extend(s["id"] for s in result["data"]["plan"]["steps"])
            cursor = result["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(len(set(ids)), 70)


class LocalViewerPlans(ViewerPlanCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown


class PostgresViewerPlans(ViewerPlanCases, NativeFixture):
    pass
