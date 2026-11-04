"""Actual daemon/socket SDK and real subprocess MCP stdio integration, without a model."""

import io
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import test_knowledge as knowledge_helpers
import test_retrieval as retrieval_helpers
from test_working import checkpoint

from memoryking.adapters.local_files import atomic
from memoryking.adapters.local_ipc import Server
from memoryking.adapters.stdio import INPUT_LIMIT, StdioBridge
from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import LocalError
from memoryking.sdk import LocalClient


def rpc(identifier, method, params=None):
    result = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if params is not None:
        result["params"] = params
    return result


def initialize():
    return rpc(
        1,
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test-host", "version": "1"},
        },
    )


class SDKTests(unittest.TestCase):
    fails = retrieval_helpers.RetrievalTests.fails

    def setUp(self):
        knowledge_helpers.KnowledgeTests.setUp(self)
        self.credential = self.control / "owner.credential"
        self.server = Server(self.store)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.client = LocalClient(self.server.path, self.credential, "p")

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        knowledge_helpers.KnowledgeTests.tearDown(self)

    def test_atlas_real_socket_and_sdk_uses_host_inspection_surface(self):
        result = self.client.atlas(bindings=[{}], max_nodes=10)
        self.assertEqual(result["nodes"][0]["id"], self.obs["observation"])
        self.assertEqual(result["binding"]["principal"], "owner")
        self.assertEqual(result["count_scope"], "returned_page_only")
        self.fails(
            "invalid_tool_arguments",
            lambda: self.client.tool(
                "memory_fetch",
                {
                    "version": 1,
                    "incarnation": self.store.incarnation,
                    "action": "atlas",
                    "args": {"project": "p", "bindings": [{}]},
                },
            ),
        )

    def test_experience_real_socket_and_provider_input(self):
        from tools.experience_demo import exercise

        self.assertTrue(exercise(self.client, self.root)["passed"])

    def test_feedback_tool_cannot_activate_or_inspect_procedures(self):
        self.fails(
            "invalid_tool_arguments",
            lambda: self.client.tool(
                "memory_feedback",
                {
                    "version": 1,
                    "incarnation": self.store.incarnation,
                    "action": "query",
                    "args": {"project": "p", "bindings": [{}], "diagnostic": True},
                },
            ),
        )

    def test_real_stdio_feedback_preserves_unknown_result(self):
        command = json.loads(
            (
                Path(__file__).resolve().parents[1] / "fixtures/contracts/experience-vectors.json"
            ).read_text()
        )[0]["request"]
        command["incarnation"] = self.store.incarnation
        command["args"]["project"] = "p"
        frames = [
            initialize(),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            rpc(2, "tools/call", {"name": "memory_feedback", "arguments": command}),
        ]
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "memoryking.adapters.stdio",
                "--socket",
                str(self.server.path),
                "--credential",
                str(self.credential),
                "--project",
                "p",
            ],
            input=b"".join(canonical_bytes(frame) + b"\n" for frame in frames),
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout.splitlines()[-1])["result"]
        self.assertFalse(result["isError"])
        record = json.loads(result["content"][0]["text"])
        self.assertEqual(record["kind"], "outcome")
        latest = self.client.experience(
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": "record",
                "args": {"project": "p", "kind": "outcome", "record": record["record"]},
            }
        )
        self.assertEqual(latest["body"]["result"], "unknown")

    def command(self, action="resume", **kw):
        args = {
            "project": "p",
            "task": "task",
            "run": None,
            "checkpoint": None,
            "bindings": [{}],
            "max_output_tokens": 6000,
            "tokenizer": "cl100k_base",
        }
        return {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": action,
            "args": args,
            **kw,
        }

    def save_command(self):
        return self.command(
            "save",
            key="save",
            args={
                "project": "p",
                "task": "task",
                "run": None,
                "expected_revision": 0,
                "readers": ["owner"],
                "checkpoint": checkpoint(),
            },
        )

    def test_tool_calls_fill_session_defaults(self):
        status = self.client.tool("memory_status", {})
        self.assertEqual(status["project"], "p")
        captured = self.client.tool(
            "memory_capture",
            {
                "key": "minimal",
                "request": {
                    "source": "sdk",
                    "source_event": "minimal",
                    "text": "durable note saved with defaults",
                    "readers": None,
                    "classification": "internal",
                    "observed_at_ms": 1,
                },
            },
        )
        self.assertIn("observation", captured)
        brief = self.client.tool("memory_brief", {"action": "brief", "args": {"query": "durable"}})
        self.assertIn("receipt", brief)
        self.assertEqual(brief["usage"]["max_output_tokens"], 6000)
        self.assertIn(captured["observation"], [i["reference"] for i in brief["guidance"]])
        receipt = self.client.tool(
            "memory_fetch", {"action": "receipt", "args": {"receipt": brief["receipt"]}}
        )
        self.assertEqual(receipt["receipt"], brief["receipt"])
        # Explicit values still win over defaults.
        explicit = self.client.tool(
            "memory_brief",
            {"action": "brief", "args": {"query": "durable", "max_output_tokens": 2000}},
        )
        self.assertEqual(explicit["usage"]["max_output_tokens"], 2000)

    def test_minimal_checkpoint_saves_in_one_call_and_resume_names_the_gaps(self):
        saved = self.client.tool(
            "memory_checkpoint",
            {
                "action": "save",
                "args": {
                    "task": "quick",
                    "checkpoint": {
                        "objective": "Finish the retry fix",
                        "current_state": "Patch drafted; tests not run",
                        "next_actions": ["Run the regression"],
                    },
                },
            },
        )
        self.assertEqual(saved["revision"], 1)
        # A second minimal save without expected_revision accepts the current revision.
        again = self.client.tool(
            "memory_checkpoint",
            {
                "action": "save",
                "args": {
                    "task": "quick",
                    "checkpoint": {
                        "objective": "Finish the retry fix",
                        "current_state": "Regression passed",
                    },
                },
            },
        )
        self.assertEqual(again["revision"], 2)
        resumed = self.client.tool(
            "memory_resume",
            {"action": "resume", "args": {"task": "quick", "checkpoint": again["checkpoint"]}},
        )
        state = resumed["checkpoint"]["state"]
        gaps = resumed.get("recovery_gaps") or state["capture"]["gaps"]
        self.assertIn("defaulted:findings", gaps)
        self.assertIn("defaulted:repository", gaps)
        self.assertIn("defaulted:next_actions", gaps)
        self.assertEqual(state["objective"], "Finish the retry fix")
        self.assertEqual(state["current_state"], "Regression passed")
        with self.assertRaises(LocalError) as caught:
            self.client.tool(
                "memory_checkpoint",
                {"action": "save", "args": {"task": "quick", "checkpoint": {"objective": "x"}}},
            )
        self.assertEqual("invalid_tool_arguments", caught.exception.code)

    def test_lean_profile_round_trip_through_five_flat_tools(self):
        from memoryking import lean
        from memoryking.adapters.stdio import LEAN_VALIDATORS

        self.assertEqual(
            [t["name"] for t in lean.TOOLS],
            ["remember", "recall", "save_progress", "resume", "hand_off"],
        )
        self.assertLess(len(json.dumps(lean.CATALOG, separators=(",", ":"))), 8192)
        for tool in lean.TOOLS:
            self.assertNotIn("oneOf", json.dumps(tool["inputSchema"]))
        first = lean.call(
            self.client,
            "remember",
            {"text": "retry backoff exceeded the deadline", "source": "pytest"},
        )
        again = lean.call(
            self.client,
            "remember",
            {"text": "retry backoff exceeded the deadline", "source": "pytest"},
        )
        self.assertEqual(first, again)
        brief = lean.call(self.client, "recall", {"query": "deadline"})
        self.assertEqual(brief["format"], "text")
        self.assertIn("retry backoff exceeded the deadline", brief["text"])
        saved = lean.call(
            self.client,
            "save_progress",
            {
                "task": "lean-task",
                "objective": "Fix the retry deadline",
                "current_state": "Investigating",
                "next_actions": ["Run the regression"],
                "failed_attempts": [{"action": "Raise every timeout", "outcome": "Still failed"}],
                "blockers": ["Need a full regression result"],
                "share_with": ["reviewer"],
            },
        )
        self.assertEqual(saved["revision"], 1)
        resumed = lean.call(self.client, "resume", {"task": "lean-task"})
        self.assertEqual(resumed["format"], "text")
        self.assertIn("# Resume: Fix the retry deadline", resumed["text"])
        self.assertIn("## Do not repeat", resumed["text"])
        self.assertIn("Need a full regression result", resumed["text"])
        self.assertIn("defaulted:references", resumed["text"])
        self.store.grant(self.owner, "reviewer", "p", ["read"])
        handed = lean.call(self.client, "hand_off", {"task": "lean-task", "to": "reviewer"})
        self.assertEqual(handed["status"], "prepared")
        self.assertTrue(LEAN_VALIDATORS["recall"].is_valid({"query": "x"}))
        self.assertFalse(LEAN_VALIDATORS["recall"].is_valid({"query": "x", "extra": 1}))

    def test_lean_bridge_lists_five_tools_over_stdio(self):
        frames = [
            initialize(),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            rpc(2, "tools/list"),
            rpc(
                3, "tools/call", {"name": "remember", "arguments": {"text": "from the lean bridge"}}
            ),
            rpc(4, "tools/call", {"name": "recall", "arguments": {"query": "lean bridge"}}),
        ]
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "memoryking.adapters.stdio",
                "--socket",
                str(self.server.path),
                "--credential",
                str(self.credential),
                "--project",
                "p",
                "--profile",
                "lean",
            ],
            input=b"".join(canonical_bytes(frame) + b"\n" for frame in frames),
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        replies = [json.loads(line) for line in run.stdout.splitlines()]
        listed = next(r for r in replies if r.get("id") == 2)["result"]["tools"]
        self.assertEqual(len(listed), 5)
        recalled = next(r for r in replies if r.get("id") == 4)["result"]
        self.assertFalse(recalled["isError"])
        self.assertIn("from the lean bridge", json.loads(recalled["content"][0]["text"])["text"])

    def test_bridge_binds_incarnation_when_a_tool_call_omits_it(self):
        result = self.client.tool(
            "memory_brief",
            {
                "version": 1,
                "action": "brief",
                "args": {
                    "project": "p",
                    "query": "durable",
                    "bindings": [{}],
                    "max_output_tokens": 6000,
                    "tokenizer": "cl100k_base",
                },
            },
        )
        self.assertEqual(result["incarnation"], self.store.incarnation)
        with self.assertRaises(LocalError) as caught:
            self.client.tool(
                "memory_brief",
                {
                    "version": 1,
                    "incarnation": "hist_" + "0" * 32,
                    "action": "brief",
                    "args": {
                        "project": "p",
                        "query": "durable",
                        "bindings": [{}],
                        "max_output_tokens": 6000,
                        "tokenizer": "cl100k_base",
                    },
                },
            )
        self.assertEqual("history_changed", caught.exception.code)

    def test_capture_key_is_idempotent_across_client_instances(self):
        def observation(event, text):
            return {
                "version": 1,
                "project": "p",
                "source": "sdk",
                "source_event": event,
                "text": text,
                "readers": None,
                "classification": "internal",
                "observed_at_ms": 1,
            }

        first = self.client.capture("same-key", observation("e1", "first bytes"))
        again = LocalClient(self.server.path, self.credential, "p").capture(
            "same-key", observation("e1", "first bytes")
        )
        self.assertEqual(first, again)
        with self.assertRaises(LocalError) as caught:
            LocalClient(self.server.path, self.credential, "p").capture(
                "same-key", observation("e1", "different bytes")
            )
        self.assertEqual("idempotency_conflict", caught.exception.code)
        other = LocalClient(self.server.path, self.credential, "p", namespace="other").capture(
            "same-key", observation("e2", "other bytes")
        )
        self.assertNotEqual(first["observation"], other["observation"])

    def test_bound_sdk_save_resume_and_no_automatic_retry(self):
        context = self.client.context()
        self.assertEqual(context["principal"], "owner")
        self.assertIn("checkpoint", context["capabilities"])
        command = self.save_command()
        first = self.client.working(command)
        self.assertFalse(first["replayed"])
        self.assertEqual(self.client.working(self.command())["checkpoint"]["state"], checkpoint())
        retry = self.client.working(command)
        self.assertTrue(retry["replayed"])
        self.assertEqual(first["checkpoint"], retry["checkpoint"])
        wrong = self.save_command()
        wrong["args"]["project"] = "different"
        self.fails("scope_mismatch", lambda: self.client.working(wrong))

    def test_sdk_rejects_identity_switch_and_scope_grant_reduction(self):
        other = self.store.grant(self.owner, "other", "p", ["read"])
        atomic(self.credential, other.encode())
        self.fails("context_changed", self.client.context)
        self.fails("context_changed", lambda: self.client.working(self.save_command()))
        fresh = LocalClient(self.server.path, self.credential, "p")
        self.assertEqual(fresh.context()["principal"], "other")
        self.assertEqual(fresh.context()["capabilities"], ["read"])
        self.store.revoke(self.owner, "other", "p")
        self.fails("denied", fresh.context)

    def test_nonadmin_context_has_no_aggregate_status_or_other_project(self):
        reader = self.store.grant(self.owner, "reader", "p", ["read"])
        file = self.control / "reader.credential"
        atomic(file, reader.encode())
        client = LocalClient(self.server.path, file, "p")
        context = client.context()
        self.assertNotIn("jobs", context)
        self.assertNotIn("position", context)
        self.assertNotIn("projection", context)
        self.fails("denied", lambda: LocalClient(self.server.path, file, "hidden"))
        self.fails("scope_mismatch", lambda: client.tool("memory_status", {"project": "hidden"}))

    def test_tool_surface_cannot_smuggle_privileged_mutations(self):
        command = self.save_command()
        command.update(
            action="assign_writer",
            args={
                "project": "p",
                "task": "task",
                "run": None,
                "expected_revision": 1,
                "writer": "owner",
                "reason": "test",
            },
        )
        self.fails("invalid_tool_arguments", lambda: self.client.tool("memory_checkpoint", command))
        self.fails("administrative_operation_required", lambda: self.client.working(command))
        self.fails("unknown_tool", lambda: self.client.tool("grant", {}))
        self.fails(
            "invalid_tool_arguments", lambda: self.client.tool("memory_resume", self.save_command())
        )

    def test_real_stdio_lifecycle_tools_checkpoint_resume_and_exact_text(self):
        messages = [
            rpc(0, "tools/list"),
            initialize(),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            rpc(2, "tools/list"),
            rpc(3, "tools/call", {"name": "memory_status", "arguments": {"project": "p"}}),
            rpc(4, "tools/call", {"name": "memory_checkpoint", "arguments": self.save_command()}),
            rpc(5, "tools/call", {"name": "memory_resume", "arguments": self.command()}),
            rpc(6, "tools/call", {"name": "grant", "arguments": {}}),
        ]
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "memoryking.adapters.stdio",
                "--socket",
                str(self.server.path),
                "--credential",
                str(self.credential),
                "--project",
                "p",
            ],
            input=b"".join(canonical_bytes(m) + b"\n" for m in messages),
            capture_output=True,
            timeout=20,
            env={
                k: v
                for k, v in os.environ.items()
                if k not in {"PYTHONPATH", "PYTHONHOME", "GMAIL_FROM", "GMAIL_APP_KEY", "KINDLE_TO"}
            },
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stderr, b"")
        responses = [json.loads(line) for line in run.stdout.splitlines()]
        self.assertEqual([r["id"] for r in responses], [0, 1, 2, 3, 4, 5, 6])
        self.assertEqual(responses[0]["error"]["message"], "not_initialized")
        names = {tool["name"] for tool in responses[2]["result"]["tools"]}
        self.assertEqual(
            names,
            {
                "memory_status",
                "memory_capture",
                "memory_brief",
                "memory_fetch",
                "memory_checkpoint",
                "memory_resume",
                "memory_plan",
                "memory_handoff",
                "memory_feedback",
            },
        )
        raw = responses[5]["result"]["content"][0]["text"].encode()
        body = json.loads(raw)
        self.assertFalse(responses[5]["result"]["isError"])
        self.assertEqual(raw, canonical_bytes(body))
        self.assertEqual(body["checkpoint"]["state"], checkpoint())
        self.assertEqual(
            self.store.db.execute(
                "SELECT body FROM rendered_artifacts WHERE id=?", (body["receipt"],)
            ).fetchone()[0],
            raw,
        )
        self.assertEqual(responses[-1]["error"]["code"], -32602)
        self.assertNotIn(self.owner, run.stdout.decode())

    def test_stdio_bad_frames_notifications_and_schema_errors_are_safe(self):
        bridge = StdioBridge(self.client)
        raw = (
            b'{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}\n' + b"x" * (INPUT_LIMIT + 1) + b"\n"
        )
        raw += canonical_bytes(initialize()) + b"\n"
        raw += b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        raw += (
            canonical_bytes(rpc(2, "tools/call", {"name": "memory_checkpoint", "arguments": {}}))
            + b"\n"
        )
        raw += (
            canonical_bytes(
                rpc(3, "tools/call", {"name": "memory_resume", "arguments": self.command()})
            )
            + b"\n"
        )
        raw += b'{"jsonrpc":"2.0","method":"unknown_notification"}\n'
        result = io.BytesIO()
        bridge.serve(io.BytesIO(raw), result)
        responses = [json.loads(line) for line in result.getvalue().splitlines()]
        self.assertEqual(len(responses), 5)
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1]["error"]["code"], -32600)
        self.assertEqual(responses[3]["error"]["code"], -32602)
        self.assertTrue(responses[4]["result"]["isError"])
        self.assertNotIn("Traceback", result.getvalue().decode())

    def test_capture_tool_and_retrieval_tool_share_current_source_authorization(self):
        captured = self.client.tool(
            "memory_capture",
            {
                "key": "sdk",
                "request": {
                    "version": 1,
                    "project": "p",
                    "source": "sdk",
                    "source_event": "sdk",
                    "text": "galactic checkpoint example",
                    "readers": ["owner"],
                    "classification": "internal",
                    "observed_at_ms": 1,
                },
            },
        )
        result = self.client.tool(
            "memory_brief",
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": "brief",
                "args": {
                    "project": "p",
                    "query": "galactic",
                    "bindings": [{}],
                    "max_output_tokens": 3000,
                    "tokenizer": "cl100k_base",
                },
            },
        )
        self.assertIn(captured["observation"], {item["reference"] for item in result["guidance"]})

    def test_changed_history_or_unavailable_socket_does_not_refresh_and_retry(self):
        old = self.client.binding
        self.store.incarnation = "hist_" + "b" * 32
        self.fails("history_changed", self.client.context)
        self.assertEqual(self.client.binding, old)
        self.store.incarnation = old[-1]
        self.client.socket = Path(self.root / "missing.sock")
        self.fails("transport_unavailable", self.client.context)

    def test_stdio_withholds_a_delayed_successful_tool_payload(self):
        bridge = StdioBridge(self.client)
        bridge.handle(initialize())
        bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        incoming = io.BytesIO(
            canonical_bytes(
                rpc(2, "tools/call", {"name": "memory_status", "arguments": {"project": "p"}})
            )
            + b"\n"
        )
        output = io.BytesIO()
        with patch("memoryking.adapters.stdio.monotonic", side_effect=[0, 5.1]):
            bridge.serve(incoming, output)
        response = json.loads(output.getvalue())
        self.assertTrue(response["result"]["isError"])
        self.assertIn("release_expired", response["result"]["content"][0]["text"])
        self.assertNotIn(self.store.store_id, output.getvalue().decode())
