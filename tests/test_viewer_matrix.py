"""Explicit differential HTTP corpus, normalized only for timing and opaque identity."""

import json
import unittest
from dataclasses import replace

import test_knowledge as knowledge
import test_viewer_http as http
from test_working import checkpoint

from memoryking.adapters.working import Working


def task(fixture, name, readers, *, proof=True):
    body = checkpoint()
    body["objective"] = "Track a visible task"
    body["current_state"] = "Producer reports progress"
    body["references"] = []
    body["verification"] = []
    body["repository"]["artifacts"] = []
    body["acceptance"] = [
        {
            "id": "proof",
            "text": "Recorded result",
            "state": "passed",
            "decision": None,
            "evidence": [{"kind": "observation", "id": fixture.obs["observation"]}],
        }
    ]
    result = Working(fixture.store).execute(
        fixture.owner,
        {
            "version": 1,
            "incarnation": fixture.store.incarnation,
            "action": "save",
            "key": name,
            "args": {
                "project": "p",
                "task": name,
                "run": None,
                "expected_revision": 0,
                "readers": readers,
                "checkpoint": body,
            },
        },
    )
    if not proof:
        body["acceptance"][0]["text"] = "Changed definition with the same old proof"
        result = Working(fixture.store).execute(
            fixture.owner,
            {
                "version": 1,
                "incarnation": fixture.store.incarnation,
                "action": "save",
                "key": name + "-changed",
                "args": {
                    "project": "p",
                    "task": name,
                    "run": None,
                    "expected_revision": 1,
                    "readers": readers,
                    "checkpoint": body,
                },
            },
        )
    return result


def normalize(value):
    if isinstance(value, dict):
        return {
            k: ("opaque" if k in {"handle", "cursor"} and v else normalize(v))
            for k, v in value.items()
            if k not in {"binding", "checkpoint_revision", "observed_ms"}
        }
    if isinstance(value, list):
        return [normalize(v) for v in value]
    return value


def corpus(fixture, scenario):
    with fixture.listener() as server:
        task(fixture, "visible", ["owner", "phone"], proof=scenario != "unknown_proof")
        task(fixture, "OWNER_ONLY_TASK", ["owner"])
        if scenario == "unknown_constraint":
            claim = fixture.propose(
                fixture.body(
                    kind="constraint",
                    selector=knowledge.sel(knowledge.eq("environment", "production")),
                    authority="project",
                    rule_key="review",
                    effect="require",
                    rule_value="yes",
                )
            )
            fixture.review(claim)
        elif scenario == "accepted_constraint":
            claim = fixture.propose(
                fixture.body(
                    kind="constraint",
                    authority="project",
                    rule_key="review",
                    effect="require",
                    rule_value="yes",
                )
            )
            fixture.review(claim)
        elif scenario == "revoked_evidence":
            fixture.store.purge(fixture.owner, fixture.obs["observation"])
        elif scenario == "missing_projection":
            fixture.store.db.execute(
                "DELETE FROM checkpoint_streams WHERE tenant=? AND project=? AND task=?",
                (fixture.tenant, "p", "visible"),
            )
        fixture.session = replace(fixture.session, subject=fixture.subject())
        result = {}
        for path in (
            "overview",
            "tasks",
            "tasks/visible",
            "tasks/OWNER_ONLY_TASK",
            "tasks/missing",
            "memories",
            "graph",
        ):
            status, headers, raw = fixture.get("/viewer/api/v1/projects/p/" + path)
            fixture.assertEqual(headers["Cache-Control"], "no-store")
            fixture.assertNotIn(b"OWNER_ONLY_TASK", raw)
            result[path] = {"status": status, "body": normalize(json.loads(raw))}
        fixture.assertEqual(result["tasks/OWNER_ONLY_TASK"], result["tasks/missing"])
        fixture.assertNotIn("internal_error", server.counts)
        if scenario in {"unknown_constraint", "revoked_evidence", "missing_projection"}:
            fixture.assertEqual(result["tasks/visible"]["status"], 404)
        else:
            detail = result["tasks/visible"]["body"]["data"]["tasks"][0]
            fixture.assertEqual(detail["progress"]["total"], 1)
            fixture.assertEqual(
                detail["criteria"][0]["state"],
                "unknown" if scenario == "unknown_proof" else "passed",
            )
        return result


class ViewerDifferential(unittest.TestCase):
    def test_identical_http_eligibility_and_coverage(self):
        http.PostgresViewerHTTP.setUpClass()
        self.addCleanup(http.PostgresViewerHTTP.tearDownClass)
        for scenario in (
            "visible",
            "unknown_proof",
            "unknown_constraint",
            "accepted_constraint",
            "revoked_evidence",
            "missing_projection",
        ):
            outputs = []
            for backend in (http.LocalViewerHTTP, http.PostgresViewerHTTP):
                fixture = backend("test_route_headers_mutation_boundary_and_non_disclosure")
                fixture.setUp()
                try:
                    outputs.append(corpus(fixture, scenario))
                finally:
                    fixture.tearDown()
            with self.subTest(scenario=scenario):
                self.assertEqual(outputs[0], outputs[1])


class ViewerGraphBounds(unittest.TestCase):
    def test_graph_and_equivalent_list_honor_configured_bounds(self):
        fixture = http.LocalViewerHTTP("test_route_headers_mutation_boundary_and_non_disclosure")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        with fixture.listener() as server:
            for i in range(3):
                task(fixture, f"task-{i}", ["owner", "phone"])
            server.config["limits"]["graph_nodes"] = 3
            server.config["limits"]["graph_edges"] = 1
            status, _, raw = fixture.get("/viewer/api/v1/projects/p/graph")
            self.assertEqual(status, 200, raw)
            result = json.loads(raw)
            self.assertLessEqual(len(result["data"]["nodes"]), 3)
            self.assertLessEqual(len(result["data"]["edges"]), 1)
            self.assertTrue(result["truncated"])
            ids = {n["id"] for n in result["data"]["nodes"]}
            self.assertEqual(len(ids), len(result["data"]["nodes"]))
            self.assertTrue(all(n["label"] and n["kind"] for n in result["data"]["nodes"]))
            for edge in result["data"]["edges"]:
                self.assertTrue({edge["source"], edge["target"]} <= ids)
