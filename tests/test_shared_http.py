"""Real local HTTP traffic against PostgreSQL, with distinct signed agent identities."""

import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest import mock

import psycopg
import uvicorn

from memoryking.adapters.local_files import atomic
from memoryking.adapters.shared_http import create_app
from memoryking.adapters.shared_service import SharedService
from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import LocalError, SystemClock
from memoryking.harness_context import AuthorizedConversation, LocalRecordingProvider
from memoryking.shared_sdk import SharedClient
from tests import test_postgres_store
from tests.test_local_store import observation


class SharedHTTPTests(unittest.TestCase):
    clock_factory = SystemClock
    setUpClass = classmethod(test_postgres_store.PostgresStoreTests.setUpClass.__func__)
    tearDownClass = classmethod(test_postgres_store.PostgresStoreTests.tearDownClass.__func__)
    store = test_postgres_store.PostgresStoreTests.store

    def setUp(self):
        test_postgres_store.PostgresStoreTests.setUp(self)
        self.tmp = tempfile.TemporaryDirectory(prefix="mk-http-", dir="/tmp")
        self.files = Path(self.tmp.name)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.port = self.listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.service = SharedService(self.pool, self.root, self.control)
        app = create_app(
            self.service,
            hosts=[f"127.0.0.1:{self.port}"],
            origins=[self.url],
            loopback_http=True,
            dashboard_tenant=self.tenant if getattr(self, "dashboard", False) else None,
        )
        self.server = uvicorn.Server(
            uvicorn.Config(app, log_level="critical", access_log=False, lifespan="on")
        )
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.listener]})
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started:
            if time.monotonic() >= deadline:
                raise RuntimeError("local HTTP start timed out")
            time.sleep(0.01)
        self.credential_path = self.files / "owner.credential"
        atomic(self.credential_path, self.credential.encode())
        self.client = SharedClient(
            self.url, self.tenant, self.credential_path, "demo", loopback_http=True
        )

    def tearDown(self):
        self.server.should_exit = True
        self.thread.join(5)
        self.listener.close()
        self.tmp.cleanup()

    def test_private_viewer_operations_share_the_release_gate(self):
        from memoryking.application.viewer import AUDIENCE

        backend = self.command(
            "grant",
            dict(principal="viewer_backend", project="demo", capabilities=["viewer_exchange"]),
        )["credential"]
        self.command(
            "grant", dict(principal="viewer_subject", project="demo", capabilities=["read"])
        )
        common = dict(
            installation_id="test", provider="github.com", starts=None, expires=None, enabled=True
        )
        self.command(
            "viewer_registry",
            {**common, "delegator": "viewer_backend", "project": "demo", "audience": AUDIENCE},
        )
        self.command("viewer_mapping", {**common, "github_id": "123", "subject": "viewer_subject"})
        self.command(
            "capture",
            dict(
                namespace=self.namespace,
                key="viewer1",
                request=observation(1, readers=["viewer_subject"]),
            ),
        )
        lease = self.command(
            "viewer_exchange",
            dict(
                installation_id="test",
                audience=AUDIENCE,
                project="demo",
                provider="github.com",
                github_id="123",
                session_ref="a" * 64,
            ),
            backend,
        )
        args = dict(lease_id=lease["lease_id"], session_ref="a" * 64, query="", cursor="")
        result = self.command("viewer_read", args, backend)
        self.assertEqual(1, len(result["data"]["items"]))
        self.assertNotIn("handle", result["data"]["items"][0])
        self.assertTrue(
            self.command(
                "viewer_close", dict(lease_id=lease["lease_id"], session_ref="a" * 64), backend
            )["closed"]
        )
        status, refused = self.raw(
            dict(version=1, operation="viewer_read", args=args),
            {"Authorization": "Bearer " + backend},
        )
        self.assertEqual(409, status)
        self.assertEqual("lease_invalid", refused["error"])
        self.assertNotIn(lease["lease_id"], json.dumps(refused))

    def raw(self, body, headers=None, tenant=None, timeout=5):
        request = urllib.request.Request(
            self.url + f"/v1/tenants/{tenant or self.tenant}/operations",
            canonical_bytes(body),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.credential,
                **(headers or {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def command(self, operation, args, credential=None):
        return self.client._request(credential or self.credential, operation, args)

    def test_plans_sdk_over_actual_http_and_native_postgres(self):
        from tools.plans_demo import exercise

        self.assertEqual(exercise(self.client)["work_percent"], 50)

    def test_experience_sdk_real_http_and_actual_provider_input(self):
        from tools.experience_demo import exercise

        self.assertTrue(exercise(self.client, self.files)["passed"])

    def test_embedding_sdk_real_http_and_native_store(self):
        captured = self.client.capture("embedding", observation())

        def execute(action, args, **extra):
            return self.client.embedding(
                {
                    "version": 1,
                    "incarnation": self.client.binding[-1],
                    "action": action,
                    "args": {"project": "demo", **args},
                    **extra,
                }
            )

        config = execute(
            "configure",
            {"model": "minilm-l6-v2-onnx", "state": "experimental", "expected_generation": 0},
            key="config",
        )
        job = execute("claim", {"fingerprint": config["fingerprint"]})
        self.assertEqual(job["observation"], captured["observation"])
        self.assertEqual(job["egress"], "local_cpu_only")
        result = execute(
            "complete",
            {
                "lease": job["lease"],
                "fingerprint": job["fingerprint"],
                "input_hash": job["input_hash"],
                "vector": [1000000] + [0] * 383,
                "input_tokens": 10,
                "truncated": False,
                "duration_ms": 5,
            },
        )
        self.assertEqual(result["status"], "complete")
        self.assertTrue(execute("status", {"fingerprint": config["fingerprint"]})["ready"])

    def test_capture_context_distinct_principals_and_host_origin_rules(self):
        first = self.client.capture("one", observation())
        reader = self.command(
            "grant", {"principal": "reviewer", "project": "demo", "capabilities": ["read"]}
        )["credential"]
        reader_path = self.files / "reader.credential"
        atomic(reader_path, reader.encode())
        client = SharedClient(self.url, self.tenant, reader_path, "demo", loopback_http=True)
        self.assertEqual(client.context()["principal"], "reviewer")
        result = client._request(
            reader, "read", {"project": "demo", "observation": first["handle"]}
        )
        self.assertEqual("durable memory", result["items"][0]["text"])
        with self.assertRaises(LocalError):
            client.capture("bad", observation(2))
        read = {"version": 1, "operation": "context", "args": {"project": "demo"}}
        self.assertEqual(200, self.raw(read)[0])  # Missing Origin is legitimate bearer traffic.
        self.assertEqual(403, self.raw(read, {"Origin": "https://hostile.invalid"})[0])
        self.assertEqual(421, self.raw(read, {"Host": "hostile.invalid"})[0])
        self.assertEqual(
            403, self.raw(read, {"Authorization": "", "Cookie": "session=anything"})[0]
        )
        self.assertEqual("invalid_request", self.raw({**read, "credential": reader})[1]["error"])

    def test_housekeeping_runs_on_its_own_pass_without_a_request_and_logs_a_failure(self):
        # An expired replay result and an artifact tombstone older than its grace are
        # released by the service's own pass; no request from the tenant is involved.
        self.client.capture("old", observation())
        now = int(time.time() * 1000)
        with self.store() as store, store.transaction():
            store.db.execute("UPDATE retries SET expires=? WHERE tenant=?", (now - 1, self.tenant))
            store.db.execute(
                "INSERT INTO rendered_artifacts VALUES (?,?,?,?,?,?,?,?,NULL,?,NULL,NULL,'[]')",
                (self.tenant, "old", "owner", "demo", store.incarnation, 1, 2, "expired", "h" * 64),
            )
        self.assertEqual(0, self.service.housekeeping_pass())
        with self.store() as store, store.transaction():
            self.assertEqual(
                0,
                store.db.execute(
                    "SELECT count(*) FROM retries WHERE tenant=? AND result IS NOT NULL",
                    (self.tenant,),
                ).fetchone()[0],
            )
            self.assertEqual(
                0,
                store.db.execute(
                    "SELECT count(*) FROM rendered_artifacts WHERE tenant=?", (self.tenant,)
                ).fetchone()[0],
            )
        self.assertIn(self.tenant, self.service._housekeeping)
        # A failing tenant is logged, counted, and does not stop the pass.
        with (
            mock.patch.object(self.service, "maintain", side_effect=RuntimeError("boom")),
            self.assertLogs("memoryking", level="ERROR") as logs,
        ):
            self.assertEqual(len(self.service.tenants()), self.service.housekeeping_pass())
        self.assertIn("event=internal_error", logs.output[0])
        self.assertNotIn("boom", logs.output[0])
        # The thread form runs a first pass right away and stops with the service.
        passes = self.service.housekeeping_passes
        thread = self.service.start_housekeeping(interval_s=3600)
        deadline = time.monotonic() + 10
        while self.service.housekeeping_passes == passes and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertGreater(self.service.housekeeping_passes, passes)
        self.service.stopping.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_client_methods_over_https_match_the_local_surface(self):
        saved = self.client.remember("shared retry finding", source="pytest")
        self.assertEqual(self.client.remember("shared retry finding", source="pytest"), saved)
        brief = self.client.recall("retry finding")
        self.assertEqual(brief.sources[0]["id"], saved.observation)
        progress = self.client.save_progress("shared-task", "Fix retries", "Investigating")
        self.assertEqual(progress.revision, 1)
        self.assertIn("Investigating", self.client.resume("shared-task").text)
        with self.assertRaises(LocalError) as caught:
            self.client.hand_off("shared-task", "nobody")
        self.assertTrue(caught.exception.hint)

    def test_missing_tenant_answers_like_a_bad_credential_for_an_existing_one(self):
        read = {"version": 1, "operation": "context", "args": {"project": "demo"}}
        with self.assertNoLogs("memoryking", level="ERROR"):
            missing = self.raw(read, tenant="t_" + uuid.uuid4().hex)
            wrong = self.raw(read, {"Authorization": "Bearer " + self.credential[:-4] + "0000"})
        self.assertEqual(wrong, missing)
        self.assertEqual(400, missing[0])
        self.assertEqual("invalid_token", missing[1]["error"])

    def test_database_lock_timeout_deadlock_and_cancel_answer_service_busy_429(self):
        read = {"version": 1, "operation": "context", "args": {"project": "demo"}}
        for error in (
            psycopg.errors.LockNotAvailable,
            psycopg.errors.QueryCanceled,
            psycopg.errors.DeadlockDetected,
            psycopg.errors.SerializationFailure,
        ):
            with (
                self.subTest(error.__name__),
                mock.patch.object(self.service, "execute", side_effect=error("SECRET sql text")),
                self.assertLogs("memoryking", level="WARNING") as logs,
            ):
                status, body = self.raw(read)
            self.assertEqual(429, status)
            self.assertEqual("service_busy", body["error"])
            self.assertNotIn("request_id", body)
            self.assertEqual(["WARNING"], [record.levelname for record in logs.records])
            self.assertNotIn("SECRET", logs.output[0])

    def test_real_lock_wait_in_a_transaction_answers_service_busy_429(self):
        with psycopg.connect(self.cluster.dsn(), autocommit=False) as blocker:
            blocker.execute(
                "SELECT 1 FROM memoryking.tenant_gates WHERE tenant=%s FOR UPDATE", (self.tenant,)
            )
            started = time.monotonic()
            status, body = self.raw(
                {"version": 1, "operation": "context", "args": {"project": "demo"}}, timeout=15
            )
            blocker.rollback()
        self.assertEqual((429, "service_busy"), (status, body["error"]))
        self.assertLess(time.monotonic() - started, 8)

    def test_rotation_over_http_retires_the_old_credential_and_keeps_the_grant(self):
        args = {"principal": "rotating", "project": "demo", "capabilities": ["read"]}
        first = self.command("grant", args)["credential"]
        self.assertEqual(first, self.command("grant", args)["credential"])
        rotated = self.command("grant", {**args, "rotate": True})["credential"]
        self.assertNotEqual(first, rotated)
        context = self.command("context", {"project": "demo"}, rotated)
        self.assertEqual("rotating", context["principal"])
        with self.assertRaises(LocalError) as caught:
            self.command("context", {"project": "demo"}, first)
        self.assertEqual("credential_replaced", caught.exception.code)
        self.assertEqual(rotated, self.command("grant", args)["credential"])

    def test_delegation_live_intersection_expiry_audience_and_context_switch(self):
        delegator = self.command(
            "grant",
            {"principal": "planner", "project": "demo", "capabilities": ["read", "capture"]},
        )["credential"]
        recipient = self.command(
            "grant",
            {"principal": "implementer", "project": "demo", "capabilities": ["read", "capture"]},
        )["credential"]
        delegated = self.command(
            "delegate",
            {
                "principal": "implementer",
                "project": "demo",
                "capabilities": ["read"],
                "ttl_ms": 60000,
            },
            delegator,
        )["credential"]
        context = self.command("context", {"project": "demo"}, delegated)
        self.assertEqual(["read"], context["capabilities"])
        self.assertEqual(1, len(context["delegation_chain"]))
        path = self.files / "delegate.credential"
        atomic(path, delegated.encode())
        client = SharedClient(self.url, self.tenant, path, "demo", loopback_http=True)
        atomic(path, recipient.encode())
        with self.assertRaises(LocalError) as caught:
            client.context()
        self.assertEqual("context_changed", caught.exception.code)
        self.command("revoke", {"principal": "planner", "project": "demo"})
        with self.assertRaises(LocalError):
            self.command("context", {"project": "demo"}, delegated)
        with self.service.store(self.tenant, self.credential) as store:
            body = store.control.verify(recipient)
            body["audience"] = "another-service"
            wrong = store.control.sign(body)
        with self.assertRaises(LocalError) as caught:
            self.command("context", {"project": "demo"}, wrong)
        self.assertEqual("credential_audience_mismatch", caught.exception.code)

    def test_actual_provider_request_rebuild_after_revocation(self):
        captured = self.client.capture("evidence", observation(text="restricted original"))
        context = AuthorizedConversation(
            self.client, LocalRecordingProvider(self.files / "provider")
        )
        packet = self.client.retrieval(
            {
                "version": 1,
                "incarnation": self.client.binding[-1],
                "action": "fetch",
                "args": {
                    "project": "demo",
                    "kind": "observation",
                    "reference": captured["observation"],
                    "start_byte": 0,
                    "end_byte": len("restricted original"),
                    "max_output_tokens": 6000,
                    "tokenizer": "cl100k_base",
                },
            }
        )
        # The harness keeps exact server artifact bytes and rechecks current authorization.
        context.add_memory(packet)
        self.command("purge", {"observation": captured["observation"]})
        context.add_host(
            "new safe input",
            authorization_id="input",
            classification="internal",
            expires_ms=int(time.time() * 1000) + 60000,
            approved=True,
        )
        context.dispatch()
        requests = list((self.files / "provider").glob("*"))
        self.assertTrue(requests)
        self.assertTrue(
            all(b"restricted original" not in p.read_bytes() for p in requests if p.is_file())
        )

    def add_host(self, context, text):
        context.add_host(
            text,
            authorization_id="accepted_input",
            classification="internal",
            expires_ms=int(time.time() * 1000) + 60000,
            approved=True,
        )

    def test_provider_grant_expiry_blocks_without_notification_and_rebuilds(self):
        # Two dispatches must complete well inside the window on a slow machine, and the
        # wait afterwards must land clearly past expiry.
        expires = int(time.time() * 1000) + 8000
        reader = self.command(
            "grant",
            {
                "principal": "reader",
                "project": "demo",
                "capabilities": ["read", "inference"],
                "expires": expires,
            },
        )["credential"]
        path = self.files / "reader.credential"
        atomic(path, reader.encode())
        client = SharedClient(self.url, self.tenant, path, "demo", loopback_http=True)
        provider = LocalRecordingProvider(self.files / "provider")
        context = AuthorizedConversation(client, provider)
        self.add_host(context, "restricted expiry fixture")
        first = context.dispatch()
        second = context.dispatch()
        self.assertIn(first["handle"], provider.path(second["handle"]).read_text())
        time.sleep(max(0, (expires + 250 - int(time.time() * 1000)) / 1000))
        with self.assertRaises(LocalError) as caught:
            context.dispatch()
        self.assertEqual("denied", caught.exception.code)
        self.assertEqual(2, len(list(provider.directory.glob("*.json"))))
        self.assertEqual([], context._messages)
        self.assertEqual([], context._handles)
        self.command(
            "grant",
            {"principal": "reader", "project": "demo", "capabilities": ["read", "inference"]},
        )
        self.add_host(context, "fresh currently authorized input")
        fresh = context.dispatch()
        actual = provider.path(fresh["handle"]).read_text()
        self.assertNotIn("restricted expiry fixture", actual)
        self.assertNotIn(first["handle"], actual)
        self.assertNotIn(second["handle"], actual)

    def test_provider_tenant_switch_drops_original_derived_and_handle_state(self):
        from memoryking.adapters.postgres_admin import initialize_tenant

        provider = LocalRecordingProvider(self.files / "provider")
        context = AuthorizedConversation(self.client, provider)
        self.add_host(context, "restricted original tenant")
        first = context.dispatch()
        second = context.dispatch()
        tenant = self.tenant + "_other"
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as connection:
            credential = initialize_tenant(connection, self.root, self.control, tenant)
        other = SharedClient(self.url, tenant, credential, "demo", loopback_http=True)
        context.rebind(other)
        self.add_host(context, "permitted new tenant input")
        current = context.dispatch()
        actual = provider.path(current["handle"]).read_text()
        self.assertNotIn("restricted original tenant", actual)
        self.assertNotIn(first["handle"], actual)
        self.assertNotIn(second["handle"], actual)
        self.assertEqual(tenant, other.context()["tenant"])

    def test_provider_restore_and_permission_service_outage_fail_closed(self):
        from memoryking.adapters.postgres_recovery import backup, restore

        provider = LocalRecordingProvider(self.files / "provider")
        context = AuthorizedConversation(self.client, provider)
        self.add_host(context, "restricted previous history")
        first = context.dispatch()
        archive = self.files / "archive"
        with (
            self.store() as store,
            psycopg.connect(self.cluster.dsn("mk_backup"), autocommit=True) as connection,
        ):
            backup(store, connection, archive)
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as connection:
            credential = restore(connection, self.root, self.control, self.tenant, archive)
        with self.assertRaises(LocalError) as caught:
            context.dispatch()
        self.assertEqual("history_changed", caught.exception.code)
        self.assertEqual(1, len(list(provider.directory.glob("*.json"))))
        current_client = SharedClient(self.url, self.tenant, credential, "demo", loopback_http=True)
        context.rebind(current_client)
        self.add_host(context, "permitted replacement history input")
        current = context.dispatch()
        actual = provider.path(current["handle"]).read_text()
        self.assertNotIn("restricted previous history", actual)
        self.assertNotIn(first["handle"], actual)
        self.server.should_exit = True
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        with self.assertRaises(LocalError) as caught:
            context.dispatch()
        self.assertEqual("transport_unavailable", caught.exception.code)
        self.assertEqual(2, len(list(provider.directory.glob("*.json"))))
        self.assertEqual([], context._messages)
        self.assertEqual([], context._handles)

    def test_official_mcp_sdk_initialize_list_call_and_reauthorization(self):
        import anyio
        import httpx2
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async def exercise():
            endpoint = self.url + f"/v1/tenants/{self.tenant}/projects/demo/mcp?profile=full"
            async with httpx2.AsyncClient(
                headers={"Authorization": "Bearer " + self.credential}, timeout=10
            ) as http:
                async with streamable_http_client(
                    endpoint, http_client=http, terminate_on_close=False
                ) as (read, write):
                    async with ClientSession(read, write, read_timeout_seconds=10) as session:
                        initialized = await session.initialize()
                        self.assertEqual("2025-11-25", initialized.protocol_version)
                        listed = await session.list_tools()
                        self.assertEqual(9, len(listed.tools))
                        status = await session.call_tool("memory_status", {"project": "demo"})
                        self.assertFalse(status.is_error)
                        body = json.loads(status.content[0].text)
                        self.assertEqual(self.tenant, body["tenant"])
                        captured = await session.call_tool(
                            "memory_capture", {"key": "mcp", "request": observation(8)}
                        )
                        self.assertFalse(captured.is_error)
                        briefed = await session.call_tool(
                            "memory_brief", {"action": "brief", "args": {"query": "durable"}}
                        )
                        self.assertFalse(briefed.is_error, briefed.content)
                        outcome = json.loads(
                            (
                                Path(__file__).resolve().parents[1]
                                / "fixtures/contracts/experience-vectors.json"
                            ).read_text()
                        )[0]["request"]
                        outcome["incarnation"] = body["incarnation"]
                        feedback = await session.call_tool("memory_feedback", outcome)
                        self.assertFalse(feedback.is_error)
                        self.assertEqual(json.loads(feedback.content[0].text)["kind"], "outcome")
                        forbidden = await session.call_tool("memory_status", {"project": "other"})
                        self.assertTrue(forbidden.is_error)
                        self.assertEqual(
                            "scope_mismatch", json.loads(forbidden.content[0].text)["error"]
                        )

        anyio.run(exercise)

    def test_lean_is_the_default_profile_over_http_mcp_with_a_markdown_reply(self):
        import anyio
        import httpx2
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async def exercise():
            endpoint = self.url + f"/v1/tenants/{self.tenant}/projects/demo/mcp"
            async with httpx2.AsyncClient(
                headers={"Authorization": "Bearer " + self.credential}, timeout=10
            ) as http:
                async with streamable_http_client(
                    endpoint, http_client=http, terminate_on_close=False
                ) as (read, write):
                    async with ClientSession(read, write, read_timeout_seconds=10) as session:
                        initialized = await session.initialize()
                        self.assertIn("recall", initialized.instructions)
                        self.assertEqual(
                            __import__("memoryking").__version__, initialized.server_info.version
                        )
                        listed = await session.list_tools()
                        names = {tool.name for tool in listed.tools}
                        self.assertIn("recall", names)
                        self.assertNotIn("memory_brief", names)
                        self.assertNotIn("confirm", names)  # chat review is not served here
                        saved = await session.call_tool("remember", {"text": "http lean memory"})
                        self.assertFalse(saved.is_error)
                        self.assertTrue(saved.content[0].text.startswith("Saved memory as obs_"))
                        self.assertIsNone(saved.structured_content)  # the text alone
                        again = await session.call_tool("remember", {"text": "http lean memory"})
                        self.assertTrue(again.content[0].text.startswith("Already saved as obs_"))
                        brief = await session.call_tool("recall", {"query": "lean memory"})
                        self.assertFalse(brief.is_error)
                        self.assertTrue(brief.content[0].text.startswith("# Briefing: lean memory"))
                        self.assertIsNone(brief.structured_content)
                        self.assertRegex(
                            brief.content[0].text, r"\n\n\[receipt rcpt_\w+; status \w+"
                        )
                        bad = await session.call_tool("recall", {})
                        self.assertTrue(bad.is_error)
                        self.assertEqual("$.query", bad.structured_content["path"])
                        self.assertEqual(["query"], bad.structured_content["missing"])

        anyio.run(exercise)

    def test_unknown_profile_is_refused_and_older_protocol_revisions_are_accepted(self):
        endpoint = self.url + f"/v1/tenants/{self.tenant}/projects/demo/mcp"
        body = canonical_bytes({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})

        def send(version, query=""):
            request = urllib.request.Request(
                endpoint + query,
                body,
                headers={
                    "Authorization": "Bearer " + self.credential,
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": version,
                },
            )
            try:
                response = urllib.request.urlopen(request, timeout=5)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                return response.status, response.read()

        from memoryking.adapters.mcp_common import HANDSHAKE_VERSIONS

        for version in HANDSHAKE_VERSIONS:
            self.assertEqual(200, send(version)[0], version)
        self.assertEqual(
            "unknown_profile", json.loads(send("2025-11-25", "?profile=x")[1])["error"]
        )

    def test_the_stateless_2026_07_28_revision_lists_and_calls_tools(self):
        """The pinned library serves the per-request-envelope revision; so does the endpoint."""
        endpoint = self.url + f"/v1/tenants/{self.tenant}/projects/demo/mcp"
        envelope = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"},
        }

        def send(method, params, name=None):
            body = canonical_bytes(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": method,
                    "params": {**params, "_meta": envelope},
                }
            )
            headers = {
                "Authorization": "Bearer " + self.credential,
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": method,
            }
            if name:
                headers["Mcp-Name"] = name
            request = urllib.request.Request(endpoint, body, headers=headers)
            try:
                response = urllib.request.urlopen(request, timeout=5)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                return response.status, json.loads(response.read())

        status, listed = send("tools/list", {})
        self.assertEqual(200, status, listed)
        self.assertIn("recall", {t["name"] for t in listed["result"]["tools"]})
        status, called = send(
            "tools/call", {"name": "remember", "arguments": {"text": "stateless call"}}, "remember"
        )
        self.assertEqual(200, status, called)
        self.assertTrue(called["result"]["content"][0]["text"].startswith("Saved memory as"))

    def test_an_mcp_tool_call_makes_one_context_decision_in_both_profiles(self):
        import anyio
        import httpx2
        import mcp_types as types
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        def receipts():
            with self.store() as store:
                with store.transaction():
                    return store.db.execute("SELECT count(*) FROM receipts").fetchone()[0]

        seen = {}

        async def exercise(profile, status_call, brief_call):
            endpoint = self.url + f"/v1/tenants/{self.tenant}/projects/demo/mcp?profile={profile}"
            async with httpx2.AsyncClient(
                headers={"Authorization": "Bearer " + self.credential}, timeout=10
            ) as http:
                async with streamable_http_client(
                    endpoint, http_client=http, terminate_on_close=False
                ) as (read, write):
                    async with ClientSession(read, write, read_timeout_seconds=10) as session:
                        await session.initialize()
                        await session.list_tools()  # lets the initialized notification settle

                        async def spent(call):
                            before = await anyio.to_thread.run_sync(receipts)
                            result = await call()
                            self.assertFalse(result.is_error, getattr(result, "content", None))
                            return await anyio.to_thread.run_sync(receipts) - before

                        async def listing():
                            await session.list_tools()
                            return types.CallToolResult(content=[])

                        seen[profile] = {"list": await spent(listing)}
                        if status_call:
                            seen[profile]["status"] = await spent(
                                lambda: session.call_tool(*status_call)
                            )
                        seen[profile]["brief"] = await spent(lambda: session.call_tool(*brief_call))

        anyio.run(exercise, "lean", None, ("recall", {"query": "durable"}))
        anyio.run(
            exercise,
            "full",
            ("memory_status", {"project": "demo"}),
            ("memory_brief", {"action": "brief", "args": {"query": "durable"}}),
        )
        # The request's own context decision (what a listing costs) serves the tool: a status
        # call adds nothing to it and a briefing adds only its own receipt.
        for profile, counts in seen.items():
            self.assertEqual(counts["list"] + 1, counts["brief"], profile)
        self.assertEqual(seen["full"]["list"], seen["full"]["status"])

    def test_delegation_expiry_and_no_self_escalation(self):
        from memoryking.adapters.postgres_store import PostgresStore
        from tests.test_local_store import FakeClock

        recipient = self.command(
            "grant", {"principal": "short", "project": "demo", "capabilities": ["read", "capture"]}
        )["credential"]
        clock = FakeClock()
        clock.utc = int(time.time() * 1000)
        with PostgresStore(
            self.pool, self.root, self.control, self.tenant, self.credential, clock=clock
        ) as owner:
            delegated = owner.delegate("short", "demo", ["read"], 1000)
        with PostgresStore(
            self.pool, self.root, self.control, self.tenant, delegated, clock=clock
        ) as subject:
            context = json.loads(subject.context(delegated, "demo").body)
            self.assertEqual(["read"], context["capabilities"])
            with self.assertRaises(LocalError):
                subject.namespace(delegated, "demo")
            with self.assertRaises(LocalError):
                subject.delegate("short", "demo", ["read"], 500)
            clock.advance(1000)
            with self.assertRaises(LocalError):
                subject.context(delegated, "demo")
        self.assertNotEqual(recipient, delegated)

    def test_mcp_listing_requires_current_grant_and_supported_protocol(self):
        reader = self.command(
            "grant", {"principal": "mcp-reader", "project": "demo", "capabilities": ["read"]}
        )["credential"]
        endpoint = self.url + f"/v1/tenants/{self.tenant}/projects/demo/mcp"
        body = canonical_bytes({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})

        def send(version="2025-11-25"):
            request = urllib.request.Request(
                endpoint,
                body,
                headers={
                    "Authorization": "Bearer " + reader,
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": version,
                },
            )
            try:
                response = urllib.request.urlopen(request, timeout=5)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                return response.status, response.read()

        self.assertEqual(200, send()[0])
        self.assertEqual(403, send("unsupported")[0])
        self.command("revoke", {"principal": "mcp-reader", "project": "demo"})
        code, raw = send()
        self.assertEqual(403, code)
        self.assertEqual("denied", json.loads(raw)["error"])
        self.assertNotIn(b"memory_capture", raw)


class ReleaseBoundaryTests(unittest.TestCase):
    def test_delay_between_headers_and_body_never_emits_expired_content(self):
        import anyio

        from memoryking.adapters.shared_http import ReleaseResponse
        from memoryking.domain.local import Decision, Released
        from tests.test_local_store import FakeClock

        clock = FakeClock()
        packet = Released(
            b'{"secret":"restricted"}', Decision(clock.utc, clock.mono, clock.utc + 5000), clock
        )
        sent = []

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.start":
                clock.advance(5001)

        async def receive():
            return {"type": "http.disconnect"}

        async def exercise():
            await ReleaseResponse(packet)({"type": "http"}, receive, send)

        anyio.run(exercise)
        self.assertEqual(b"", sent[-1]["body"])
